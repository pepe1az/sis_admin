"""Safety properties of the fault runner, without touching the real network."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('chaos', Path(__file__).resolve().parents[1] / 'scripts/chaos.py')
chaos = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chaos)


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.active = self.root / 'active.json'
        self.state_patch = patch.multiple(chaos, STATE=self.root, ACTIVE=self.active)
        self.state_patch.start()
        self.log_patch = patch.object(chaos, 'log')
        self.log_patch.start()

    def tearDown(self):
        self.log_patch.stop()
        self.state_patch.stop()
        self.temp.cleanup()

    def test_undo_reverses_changes_and_removes_state_only_after_success(self):
        plan = {'id': 'test', 'undo': [['ip', 'link', 'set', 'br0', 'up'], ['ip', 'link', 'set', 'dc1', 'mtu', '1500']]}
        self.active.write_text(json.dumps(plan))
        with patch.object(chaos, 'run') as run:
            chaos.undo(plan)
            self.assertEqual(run.call_args_list[0].args[0][-2:], ['mtu', '1500'])
        self.assertFalse(self.active.exists())

    def test_failed_undo_keeps_state_for_systemd_retry(self):
        plan = {'id': 'test', 'undo': [['ip', 'link', 'set', 'br0', 'up']]}
        self.active.write_text(json.dumps(plan))
        with patch.object(chaos, 'run', side_effect=RuntimeError('temporary failure')):
            with self.assertRaises(RuntimeError):
                chaos.undo(plan)
        self.assertTrue(self.active.exists())

    def test_old_timer_does_not_undo_a_new_experiment(self):
        self.active.write_text(json.dumps({'id': 'new', 'undo': []}))
        with patch('sys.argv', ['g2-chaos', 'rollback', '--expected-id', 'old']), patch.object(chaos.os, 'geteuid', return_value=0), patch.object(chaos, 'undo') as undo:
            chaos.main()
            undo.assert_not_called()
        self.assertTrue(self.active.exists())

    def test_no_active_state_makes_rollback_idempotent(self):
        with patch('sys.argv', ['g2-chaos', 'rollback']), patch.object(chaos.os, 'geteuid', return_value=0), patch.object(chaos, 'run') as run:
            chaos.main()
            run.assert_not_called()

    def test_excessive_duration_rejected_before_network_changes(self):
        with patch('sys.argv', ['g2-chaos', 'wan', '--seconds', '601']), patch.object(chaos.os, 'geteuid', return_value=0), patch.object(chaos, 'run') as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                chaos.main()
            run.assert_not_called()

    def test_timer_failure_prevents_fault_injection(self):
        topology = {'networks': [{'name': 'dc1', 'bridge': 'virbr-g2dc1', 'cidr': '10.22.1.0/24'}], 'vms': []}
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == '/etc/itmo-g2-topology.json':
                return json.dumps(topology)
            return original_read(path, *args, **kwargs)
        def command(argv):
            if argv[0] == 'systemd-run':
                raise RuntimeError('timer unavailable')
            if argv[:3] == ['ip', '-j', 'link']:
                return '[{"flags": ["UP"]}]'
            return 'qemu:///system'
        with patch('sys.argv', ['g2-chaos', 'switch']), patch.object(chaos.os, 'geteuid', return_value=0), patch.object(Path, 'read_text', read), patch.object(chaos, 'run', side_effect=command) as run:
            with self.assertRaises(RuntimeError):
                chaos.main()
            self.assertFalse(any(c.args[0][:3] == ['ip', 'link', 'set'] for c in run.call_args_list))
        self.assertFalse(self.active.exists())

    def test_colocated_zookeeper_isolation_covers_all_peers_and_loopback(self):
        topology = {
            'networks': [{'name': 'dc1'}],
            'vms': [
                {'name': f'g2-dc{i}', 'ip': f'10.22.{i}.10',
                 'role': 'clickhouse', 'zookeeper': True}
                for i in range(1, 4)
            ],
        }
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == '/etc/itmo-g2-topology.json':
                return json.dumps(topology)
            return original_read(path, *args, **kwargs)
        def command(argv):
            if argv == ['hostname', '-s']:
                return 'g2-dc1'
            return ''
        with patch('sys.argv', ['g2-chaos', 'isolate-zk', '--node', '10.22.1.10']), patch.object(chaos.os, 'geteuid', return_value=0), patch.object(Path, 'read_text', read), patch.object(chaos, 'run', side_effect=command) as run:
            chaos.main()
            rules = (self.root / 'rules.nft').read_text()
            for ip in ['10.22.1.10', '10.22.2.10', '10.22.3.10', '127.0.0.1']:
                self.assertIn(ip, rules)
            self.assertIn('tcp dport 2181 drop', rules)
            self.assertNotIn('2888', rules)
            self.assertNotIn('3888', rules)
            commands = [call.args[0] for call in run.call_args_list]
            timer = next(i for i, c in enumerate(commands) if c[0] == 'systemd-run')
            apply = next(i for i, c in enumerate(commands) if c[:2] == ['nft', '-f'])
            self.assertLess(timer, apply)

    def run_dc_scenario(self, first='dc1', second='dc2', fail_destroy=None, timer_failure=False):
        topology = {
            'networks': [{'name': dc} for dc in ['dc1', 'dc2', 'dc3']],
            'vms': [
                {'name': 'router', 'dc': 'wan'},
                {'name': 'one', 'dc': 'dc1'},
                {'name': 'two', 'dc': 'dc2'},
                {'name': 'three', 'dc': 'dc3'},
                {'name': 'already-off', 'dc': first},
            ],
        }
        states = {'router': 'running', 'one': 'running', 'two': 'running',
                  'three': 'running', 'already-off': 'shut off'}
        original = states.copy()
        events = []
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            if str(path) == '/etc/itmo-g2-topology.json':
                return json.dumps(topology)
            return original_read(path, *args, **kwargs)

        def command(argv):
            events.append(argv)
            if argv[0] == 'systemd-run':
                self.assertTrue(self.active.exists())
                if timer_failure:
                    raise RuntimeError('timer unavailable')
            elif argv[:2] == ['virsh', 'domstate']:
                return states[argv[2]]
            elif argv[:2] == ['virsh', 'destroy']:
                self.assertTrue(any(e[0] == 'systemd-run' for e in events))
                if argv[2] == fail_destroy:
                    raise RuntimeError('injected power-off failure')
                states[argv[2]] = 'shut off'
            elif argv[:2] == ['virsh', 'start']:
                states[argv[2]] = 'running'
            return 'qemu:///system'

        with patch('sys.argv', ['g2-chaos', 'two-dc', '--dc', first, '--peer', second]), patch.object(chaos.os, 'geteuid', return_value=0), patch.object(Path, 'read_text', read), patch.object(chaos, 'run', side_effect=command):
            if fail_destroy or timer_failure:
                with self.assertRaises(RuntimeError):
                    chaos.main()
                self.assertEqual(states, original)
                self.assertFalse(self.active.exists())
            else:
                chaos.main()
                affected = [n['name'] for n in topology['vms'] if n['dc'] in {first, second} and original[n['name']] == 'running']
                self.assertEqual([e[2] for e in events if e[:2] == ['virsh', 'destroy']], affected)
                self.assertEqual(states['router'], 'running')
                self.assertEqual(states['already-off'], 'shut off')
                plan = json.loads(self.active.read_text())
                self.assertEqual(plan['affected_dcs'], sorted([first, second]))
                self.assertEqual(plan['undo'], [['start', name] for name in affected])
                chaos.undo(plan)
                self.assertEqual(states, original)
                chaos.undo(plan)  # Recovery remains safe if retried.
                self.assertEqual(states, original)
        return events

    def test_two_dc_all_pairs_preserve_third_dc_router_and_stopped_nodes(self):
        for first, second in [('dc1', 'dc2'), ('dc1', 'dc3'), ('dc2', 'dc3')]:
            with self.subTest(first=first, second=second):
                self.run_dc_scenario(first, second)

    def test_two_dc_partial_failure_restores_original_state(self):
        self.run_dc_scenario(fail_destroy='two')

    def test_two_dc_timer_failure_prevents_shutdowns(self):
        events = self.run_dc_scenario(timer_failure=True)
        self.assertFalse(any(e[:2] == ['virsh', 'destroy'] for e in events))

    def test_two_dc_rejects_duplicate_dc_before_changes(self):
        with patch('sys.argv', ['g2-chaos', 'two-dc', '--dc', 'dc2', '--peer', 'dc2']), patch.object(chaos.os, 'geteuid', return_value=0), patch.object(chaos, 'run') as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                chaos.main()
            run.assert_not_called()
        self.assertFalse(self.active.exists())


if __name__ == '__main__':
    unittest.main()
