#!/usr/bin/env python3
"""Bounded network/DC faults. Run as root on router (network) or host (switch/DC)."""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time

STATE = Path('/var/lib/itmo-g2-chaos')
ACTIVE = STATE / 'active.json'

def log(message):
    line = time.strftime('%Y-%m-%dT%H:%M:%S%z ') + message
    print(line, flush=True)
    with open('/var/log/itmo-g2-chaos.log', 'a') as out:
        out.write(line + '\n')

def run(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f'{args}: {result.stderr.strip()}')
    return result.stdout.strip()

def undo(plan):
    for action in reversed(plan['undo']):
        kind, *args = action
        if kind == 'tc':
            current = json.loads(run(['tc', '-j', 'qdisc', 'show', 'dev', args[0]]))
            if any(q.get('handle') == '1:' for q in current):
                run(['tc', 'qdisc', 'del', 'dev', args[0], 'root'])
        elif kind == 'nft':
            tables = run(['nft', 'list', 'tables'])
            if 'table inet g2_chaos' in tables:
                run(['nft', 'delete', 'table', 'inet', 'g2_chaos'])
        elif kind == 'start':
            if run(['virsh', 'domstate', args[0]]) != 'running':
                run(['virsh', 'start', args[0]])
        else:
            run([kind, *args])
    ACTIVE.unlink(missing_ok=True)
    log('ROLLBACK OK ' + plan['id'])

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', choices=['wan', 'isolate-zk', 'asymmetric', 'mtu', 'switch', 'dc', 'two-dc', 'rollback'])
    parser.add_argument('--dc', choices=['dc1', 'dc2', 'dc3'], default='dc1')
    parser.add_argument('--peer', choices=['dc1', 'dc2', 'dc3'], default='dc2')
    parser.add_argument('--seconds', type=int, default=120)
    parser.add_argument('--delay', type=int, default=100)
    parser.add_argument('--loss', type=float, default=1)
    parser.add_argument('--corrupt', type=float, default=0.1)
    parser.add_argument('--reorder', type=float, default=1)
    parser.add_argument('--node', default='10.22.1.10')
    parser.add_argument('--expected-id')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run as root')
    if not 10 <= args.seconds <= 600 or not 50 <= args.delay <= 300:
        parser.error('seconds: 10..600; delay: 50..300 ms')
    if not all(0 <= x <= 10 for x in [args.loss, args.corrupt, args.reorder]):
        parser.error('loss/corrupt/reorder: 0..10 percent')
    if args.scenario == 'two-dc' and args.dc == args.peer:
        parser.error('--dc and --peer must differ')
    STATE.mkdir(mode=0o700, exist_ok=True)
    with open(STATE / 'lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.scenario == 'rollback':
            if ACTIVE.exists():
                plan = json.loads(ACTIVE.read_text())
                if args.expected_id is None or plan['id'] == args.expected_id:
                    undo(plan)
            return
        if ACTIVE.exists():
            parser.error('Another fault is active. Wait for rollback or run rollback first.')
        topology = json.loads(Path('/etc/itmo-g2-topology.json').read_text())
        networks = {n['name']: n for n in topology['networks']}
        nodes = topology['vms']
        dc = networks[args.dc]
        plan = {'id': f'g2-rollback-{time.time_ns()}', 'undo': [], 'apply': []}
        apply, rollback = plan['apply'], plan['undo']
        if args.scenario in ['switch', 'dc', 'two-dc']:
            run(['virsh', 'uri'])
            if args.scenario == 'switch':
                bridge = dc['bridge']
                link = json.loads(run(['ip', '-j', 'link', 'show', bridge]))[0]
                if 'UP' not in link['flags']:
                    parser.error('Bridge must initially be UP')
                rollback.append(['ip', 'link', 'set', bridge, 'up'])
                apply.append(['ip', 'link', 'set', bridge, 'down'])
            else:
                affected_dcs = {args.dc, args.peer} if args.scenario == 'two-dc' else {args.dc}
                plan['affected_dcs'] = sorted(affected_dcs)
                for vm in nodes:
                    if vm['dc'] in affected_dcs and run(['virsh', 'domstate', vm['name']]) == 'running':
                        rollback.append(['start', vm['name']])
                        apply.append(['virsh', 'destroy', vm['name']])
                if not apply:
                    parser.error('No running VMs in the selected DCs')
        else:
            if args.scenario != 'isolate-zk' and run(['hostname', '-s']) != 'g2-router':
                parser.error('Network scenarios must run on g2-router')
            iface = args.dc
            if args.scenario == 'wan':
                qdiscs = json.loads(run(['tc', '-j', 'qdisc', 'show', 'dev', iface]))
                if any(q.get('root') and q.get('handle', '0:') != '0:' for q in qdiscs):
                    parser.error('Refusing to replace an existing custom root qdisc')
                rollback.append(['tc', iface])
                apply += [
                    ['tc', 'qdisc', 'add', 'dev', iface, 'root', 'handle', '1:', 'prio', 'bands', '3', 'priomap', *(['2'] * 16)],
                    ['tc', 'qdisc', 'add', 'dev', iface, 'parent', '1:1', 'handle', '10:', 'netem',
                     'delay', f'{args.delay}ms', '10ms', 'loss', f'{args.loss}%',
                     'corrupt', f'{args.corrupt}%', 'reorder', f'{args.reorder}%']]
                for other in networks.values():
                    if other['name'] not in ['wan', args.dc]:
                        apply.append(['tc', 'filter', 'add', 'dev', iface, 'protocol', 'ip', 'parent', '1:',
                                      'prio', '1', 'u32', 'match', 'ip', 'src', other['cidr'],
                                      'match', 'ip', 'dst', dc['cidr'], 'flowid', '1:1'])
            else:
                if 'table inet g2_chaos' in run(['nft', 'list', 'tables']):
                    parser.error('Existing g2_chaos table; inspect it before retrying')
                rollback.append(['nft'])
                if args.scenario == 'isolate-zk':
                    ipaddress.IPv4Address(args.node)
                    node = next((n for n in nodes if n['ip'] == args.node and n['role'] == 'clickhouse'), None)
                    if node is None:
                        parser.error('--node must be a ClickHouse IP from topology')
                    if run(['hostname', '-s']) != node['name']:
                        parser.error('Run isolate-zk on the ClickHouse VM specified by --node')
                    zk_ips = ', '.join(n['ip'] for n in nodes if (n['role'] == 'zookeeper' or n.get('zookeeper', False)))
                    if node.get('zookeeper', False):
                        zk_ips += ', 127.0.0.1'
                    rules = ('chain output { type filter hook output priority -10; policy accept; '
                             'ip daddr { ' + zk_ips + ' } tcp dport 2181 drop; } '
                             'chain input { type filter hook input priority -10; policy accept; '
                             'ip saddr { ' + zk_ips + ' } tcp sport 2181 drop; }')
                elif args.scenario == 'asymmetric':
                    if args.dc == args.peer:
                        parser.error('--dc and --peer must differ')
                    rules = f'chain forward {{ type filter hook forward priority -10; policy accept; ip saddr {dc["cidr"]} ip daddr {networks[args.peer]["cidr"]} drop; }}'
                else:
                    old_mtu = str(json.loads(run(['ip', '-j', 'link', 'show', iface]))[0]['mtu'])
                    rollback.append(['ip', 'link', 'set', iface, 'mtu', old_mtu])
                    apply.append(['ip', 'link', 'set', iface, 'mtu', '576'])
                    rules = 'chain output { type filter hook output priority -10; policy accept; icmp type destination-unreachable icmp code frag-needed drop; }'
                rules_path = STATE / 'rules.nft'
                rules_path.write_text('table inet g2_chaos {\n' + rules.replace('} chain', '}\nchain') + '\n}\n')
                run(['nft', '-c', '-f', str(rules_path)])
                apply.append(['nft', '-f', str(rules_path)])
        ACTIVE.write_text(json.dumps(plan))
        try:
            run(['systemd-run', '--unit', plan['id'], f'--on-active={args.seconds}s', '--timer-property=AccuracySec=1s',
                 '--property=Restart=on-failure', '--property=RestartSec=5s',
                 '/usr/local/sbin/g2-chaos', 'rollback', '--expected-id', plan['id']])
        except Exception:
            ACTIVE.unlink(missing_ok=True)
            raise
        log('BEGIN ' + plan['id'] + ' ' + ' '.join(sys.argv[1:]))
        try:
            for command in apply:
                log('APPLY ' + ' '.join(command))
                run(command)
        except Exception:
            undo(plan)
            raise
        log(f'ARMED automatic rollback in {args.seconds}s; unit={plan["id"]}.timer')

if __name__ == '__main__':
    main()
