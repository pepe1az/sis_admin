"""Validate placement, disk attachment and shared service inventory offline."""
import collections
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]


class CompactTopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = yaml.safe_load((ROOT / 'group_vars/all.yml').read_text())
        cls.nodes = cls.config['lab_vms']
        cls.data_nodes = [n for n in cls.nodes if n['role'] == 'clickhouse']

    def test_memory_and_replica_failure_domains(self):
        self.assertEqual(len(self.nodes), 5)
        self.assertEqual(sum(n['memory'] for n in self.nodes), 13 * 1024)
        shards = collections.defaultdict(list)
        for node in self.data_nodes:
            self.assertEqual(node['memory'], 4096)
            self.assertTrue(node['zookeeper'])
            self.assertEqual(len(node['clickhouse_instances']), 2)
            ports = []
            for instance in node['clickhouse_instances']:
                shards[instance['shard']].append((node['dc'], instance['replica']))
                ports.extend(instance[p] for p in ['tcp_port', 'http_port', 'interserver_port'])
            self.assertEqual(len(ports), len(set(ports)))
        self.assertEqual(set(shards), {1, 2, 3})
        for replicas in shards.values():
            self.assertEqual(len(replicas), 2)
            self.assertEqual(len({dc for dc, _ in replicas}), 2)
            self.assertEqual({replica for _, replica in replicas}, {1, 2})

    def test_domain_xml_attaches_eight_distinct_disks(self):
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        template = env.from_string((ROOT / 'roles/vm/templates/domain.xml.j2').read_text())
        for node in self.data_nodes:
            domain = ET.fromstring(template.render(**self.config, vm=node, vm_uuid='test'))
            self.assertEqual(domain.find('memory').text, '4096')
            disks = domain.findall('./devices/disk[@device="disk"]')
            self.assertEqual(len(disks), 9)  # root plus two sets of four data disks
            targets = [d.find('target').attrib['dev'] for d in disks]
            self.assertEqual(len(set(targets)), 9)
            assigned = [disk for i in node['clickhouse_instances'] for disk in i['data_disks']]
            self.assertEqual(len(set(assigned)), 8)
            self.assertEqual(set(assigned), set(targets) - {'vda'})
            serials = [d.find('serial').text for d in disks]
            self.assertEqual(len(set(serials)), 9)

    def test_inventory_shares_hosts_without_duplicate_vms(self):
        inv = yaml.safe_load((ROOT / 'inventory/guests.generated.yml').read_text())['all']['children']
        expected = {n['name'] for n in self.data_nodes}
        self.assertEqual(set(inv['clickhouse']['hosts']), expected)
        self.assertEqual(set(inv['zookeeper']['hosts']), expected)
        for node in self.data_nodes:
            for role in ['clickhouse', 'zookeeper']:
                host = inv[role]['hosts'][node['name']]
                self.assertEqual(host['ansible_host'], node['ip'])
                self.assertEqual(host['clickhouse_instances'], node['clickhouse_instances'])
                self.assertEqual(host['zookeeper_id'], node['zookeeper_id'])


if __name__ == '__main__':
    unittest.main()
