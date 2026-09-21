import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SCRIPT = os.path.join(ROOT, 'tools', 'profile_locators.py')
PY = sys.executable


def _node(kind, **attrs):
    base = {'type': kind}
    base.update(attrs)
    return {'attributes': base, 'children': []}


def _run(*args):
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    return subprocess.run([PY, SCRIPT, *args], capture_output=True,
                          env=env, timeout=60)


def _write_tree(directory, name, tree):
    path = os.path.join(directory, name)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(tree, handle, ensure_ascii=False)
    return path


def _sample_tree():
    root = _node('root')
    strong = _node('Button', id='btn_login', clickable='true',
                   text='登录', bounds='[10,20][110,80]')
    weak = _node('Text', text='欢迎', clickable='true',
                 bounds='[0,0][10,10]')
    hard = _node('Image', clickable='true', accessibilityId='42',
                 hashcode='7:42', hierarchy='ROOT7,0,1',
                 bounds='[5,5][45,45]')
    plain = _node('Column', bounds='[0,0][700,1200]')
    root['children'] = [strong, weak, hard, plain]
    return root


class TestProfileLocators(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ohauto_locators_')

    def test_missing_dir_rejected(self):
        empty = tempfile.mkdtemp(prefix='ohauto_empty_')
        result = _run(empty)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未找到', result.stderr.decode('utf-8', 'replace'))

    def test_counts_by_locator_level(self):
        _write_tree(self.dir, 'a.json', _sample_tree())
        out = os.path.join(self.dir, 'stats.json')
        result = _run(self.dir, '--json', out)
        self.assertEqual(result.returncode, 0,
                         result.stderr.decode('utf-8', 'replace'))
        with open(out, encoding='utf-8') as handle:
            data = json.load(handle)
        total = data['total']
        self.assertEqual(total['nodes'], 5)
        self.assertEqual(total['interactive'], 3)
        self.assertEqual(total['strong'], 1)
        self.assertEqual(total['weak'], 1)
        self.assertEqual(total['none'], 1)

    def test_accessibility_id_is_not_a_locator(self):
        tree = _node('root')
        tree['children'] = [_node('Image', clickable='true',
                                  accessibilityId='99', hashcode='1:99')]
        _write_tree(self.dir, 'b.json', tree)
        out = os.path.join(self.dir, 'stats.json')
        _run(self.dir, '--json', out)
        with open(out, encoding='utf-8') as handle:
            total = json.load(handle)['total']
        self.assertEqual(total['none'], 1)
        self.assertEqual(total['strong'] + total['weak'], 0)

    def test_non_interactive_not_counted(self):
        tree = _node('root')
        tree['children'] = [_node('Column', id='x', bounds='[0,0][1,1]')]
        _write_tree(self.dir, 'c.json', tree)
        out = os.path.join(self.dir, 'stats.json')
        _run(self.dir, '--json', out)
        with open(out, encoding='utf-8') as handle:
            total = json.load(handle)['total']
        self.assertEqual(total['interactive'], 0)
        self.assertEqual(total['nodes'], 2)

    def test_export_hard_elements(self):
        _write_tree(self.dir, 'd.json', _sample_tree())
        out = os.path.join(self.dir, 'hard.json')
        result = _run(self.dir, '--quiet', '--export-hard', out)
        self.assertEqual(result.returncode, 0,
                         result.stderr.decode('utf-8', 'replace'))
        with open(out, encoding='utf-8') as handle:
            data = json.load(handle)
        self.assertEqual(data['count'], 1)
        element = data['elements'][0]
        self.assertEqual(element['type'], 'Image')
        self.assertEqual(element['bounds'], '[5,5][45,45]')
        self.assertIn('Image', element['path'])
        self.assertEqual(element['source'], 'd.json')

    def test_meta_json_ignored(self):
        _write_tree(self.dir, 'e.json', _sample_tree())
        _write_tree(self.dir, 'e.meta.json', {'note': 'should be ignored'})
        out = os.path.join(self.dir, 'stats.json')
        _run(self.dir, '--json', out)
        with open(out, encoding='utf-8') as handle:
            data = json.load(handle)
        self.assertEqual(data['file_count'], 1)

    def test_quiet_mode_is_json(self):
        _write_tree(self.dir, 'f.json', _sample_tree())
        result = _run(self.dir, '--quiet')
        payload = json.loads(result.stdout.decode('utf-8'))
        self.assertEqual(payload['interactive'], 3)
        self.assertEqual(payload['none'], 1)


if __name__ == '__main__':
    unittest.main()
