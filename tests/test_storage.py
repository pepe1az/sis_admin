from pathlib import Path
import importlib.util
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('storage_probe', ROOT / 'roles/storage/files/storage_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

class ManagedArraysTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / 'mdadm.conf'
        self.old_config = probe.CONFIG
        probe.CONFIG = self.config
        self.addCleanup(setattr, probe, 'CONFIG', self.old_config)
        self.first = '11111111:11111111:11111111:11111111'
        self.second = '22222222:22222222:22222222:22222222'

    def save(self, directives):
        self.config.write_text('# Preserve administrator settings\nMAILADDR root\n# BEGIN STORAGE LAB MANAGED\n' + '\n'.join(directives) + '\n# END STORAGE LAB MANAGED\n')

    def test_two_arrays_survive_alternating_runs(self):
        self.config.write_text('MAILADDR root\n')
        self.save(probe.directives({'md': '/dev/md0', 'monitor': True}, self.first))
        self.save(probe.directives({'md': '/dev/md1', 'monitor': True}, self.second))
        stable = self.config.read_text()
        for md, identity in [('/dev/md0', self.first), ('/dev/md1', self.second)] * 2:
            self.save(probe.directives({'md': md, 'monitor': True}, identity))
            lines = self.config.read_text().splitlines()
            self.assertEqual(sum(l.startswith('ARRAY ') for l in lines), 2)
            self.assertEqual(sum(l.startswith('PROGRAM ') for l in lines), 1)
            self.assertEqual(self.config.read_text(), stable)
            self.assertIn('MAILADDR root', lines)
            self.assertNotIn('\\n', self.config.read_text())

    def test_conflicting_program_is_refused(self):
        self.config.write_text('PROGRAM /some/other/program\n')
        with self.assertRaisesRegex(ValueError, 'Conflicting mdadm PROGRAM'):
            probe.directives({'md': '/dev/md0', 'monitor': True}, self.first)

    def test_wrong_uuid_is_refused(self):
        self.save([f'ARRAY /dev/md0 UUID={self.first}'])
        with self.assertRaisesRegex(ValueError, 'Conflicting mdadm ARRAY'):
            probe.directives({'md': '/dev/md0', 'monitor': True}, self.second)

    def test_duplicate_sibling_is_refused(self):
        self.save([f'ARRAY /dev/md1 UUID={self.second}'] * 2)
        with self.assertRaisesRegex(ValueError, 'Duplicate managed ARRAY'):
            probe.directives({'md': '/dev/md0', 'monitor': True}, self.first)

if __name__ == '__main__':
    unittest.main()
