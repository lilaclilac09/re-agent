from contextlib import redirect_stderr, redirect_stdout
from http.server import HTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import requests

from agent.client import REAgent
from agent.loops.triage import run_triage
from agent.notebook import Notebook
from bn_bridge import mock as bridge_mock
from bn_bridge import notes
import run_agent


class EntrypointTests(unittest.TestCase):
    def test_help(self):
        result = subprocess.run([sys.executable, 'run_agent.py', '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--mock', result.stdout)

    def test_mock_import(self):
        with patch('bn_bridge.mock.run') as run:
            run_agent._start_mock(0)
        run.assert_called_once_with(0)

    def test_missing_key(self):
        error = io.StringIO()
        with patch.object(sys, 'argv', ['run_agent.py', '--mock']), \
                patch.dict(os.environ, {'ANTHROPIC_API_KEY': ''}), \
                patch('run_agent._start_mock'), redirect_stdout(io.StringIO()), redirect_stderr(error):
            with self.assertRaises(SystemExit) as raised:
                run_agent.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn('ANTHROPIC_API_KEY not set', error.getvalue())

    def test_empty_prompt(self):
        with patch.object(sys, 'argv', ['run_agent.py']), patch('run_agent._load_prompt', return_value=''), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                run_agent.main()
        self.assertEqual(raised.exception.code, 1)

    def test_oneshot_imports_existing_packages(self):
        with patch.object(sys, 'argv', ['run_agent.py', '--oneshot', 'synthetic objective']), \
                patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'offline-placeholder'}), \
                patch('agent.client.REAgent') as agent, \
                patch('agent.loops.triage.run_single_objective', return_value='fixture response') as run, \
                redirect_stdout(io.StringIO()):
            run_agent.main()
        run.assert_called_once_with(agent.return_value, 'synthetic objective', max_turns=24)


class NotebookStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'synthetic-sample')

    def test_empty(self):
        self.assertEqual(notes.list_notes(self.path)['hypotheses'], [])
        self.assertEqual(notes.export_summary(self.path)['timeline'], [])

    def test_binary_kind_roundtrips(self):
        for kind, bucket in [('fact', 'facts'), ('hypothesis', 'hypotheses'), ('unknown', 'unknowns'), ('todo', 'todos')]:
            with self.subTest(kind=kind):
                notes.write_note(self.path, 'binary', 'binary', kind, f'synthetic {kind}')
                self.assertEqual(notes.list_notes(self.path)[bucket][0]['text'], f'synthetic {kind}')
                self.assertEqual(notes.export_summary(self.path)[bucket][0]['text'], f'synthetic {kind}')
        self.assertNotIn('hypothesiss', notes.load_notes(self.path))

    def test_function_hypothesis_export(self):
        notes.write_note(self.path, 'function', '0x1000', 'hypothesis', 'synthetic function hypothesis')
        self.assertEqual(notes.list_notes(self.path)['hypotheses'], [])
        self.assertEqual(notes.export_summary(self.path)['hypotheses'][0]['address'], '0x1000')

    def test_corrupt_sidecar_is_not_silent_success(self):
        Path(self.path + '.re_notes.json').write_text('{')
        with self.assertRaises(json.JSONDecodeError):
            notes.load_notes(self.path)


class NotebookMirrorTests(unittest.TestCase):
    def test_hypothesis_mirror(self):
        notebook = Notebook('http://127.0.0.1:1')
        self.assertEqual(notebook.hypotheses, [])
        with patch('agent.notebook.requests.post') as post:
            post.return_value.json.return_value = {'ok': True, 'data': {}}
            notebook.hypothesis('synthetic hypothesis')
        self.assertEqual(len(notebook.hypotheses), 1)
        self.assertIn('synthetic hypothesis', notebook.summary_text())

    def test_failed_write_does_not_invent_a_note(self):
        notebook = Notebook('http://127.0.0.1:1')
        with patch('agent.notebook.requests.post', side_effect=requests.ConnectionError('offline fixture')):
            with self.assertRaises(requests.ConnectionError):
                notebook.hypothesis('not stored')
        self.assertEqual(notebook.hypotheses, [])


class AgentMockBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), bridge_mock.MockHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        bridge_mock._notes_store = {
            'facts': [], 'hypotheses': [], 'unknowns': [], 'todos': [], 'timeline': [], 'function_notes': {},
        }
        bridge_mock._objective = ''
        bridge_mock._renames.clear()
        self.api = Mock()
        with patch('agent.client.anthropic.Anthropic', return_value=self.api):
            self.agent = REAgent(self.url, 'Offline test fixture', verbose=False)

    def test_tool_write_is_in_next_turn_system_notebook(self):
        self.api.messages.create.side_effect = [
            NS(stop_reason='tool_use', content=[NS(type='tool_use', id='fixture-note', name='write_note', input={
                'kind': 'hypothesis', 'scope': 'binary', 'text': 'synthetic hypothesis',
            })]),
            NS(stop_reason='end_turn', content=[NS(type='text', text='fixture response')]),
        ]
        self.assertEqual(self.agent.run('synthetic objective', max_turns=2), 'fixture response')
        self.assertEqual(len(bridge_mock._notes_store['hypotheses']), 1)
        system = self.api.messages.create.call_args_list[1].kwargs['system']
        self.assertIn('HYPOTHESES (1)', system)
        self.assertIn('synthetic hypothesis', system)

    def test_all_five_phases_get_their_own_budget(self):
        self.api.messages.create.side_effect = [
            NS(stop_reason='end_turn', content=[NS(type='text', text=f'fixture phase {i}')]) for i in range(1, 6)
        ]
        with redirect_stdout(io.StringIO()):
            report = run_triage(self.agent, objective='synthetic objective', max_turns_per_phase=1)
        self.assertEqual(report, 'fixture phase 5')
        self.assertEqual(self.api.messages.create.call_count, 5)
        self.assertIn('synthetic objective', self.api.messages.create.call_args.kwargs['system'])

    def test_zero_budget(self):
        self.assertIn('budget (0) exhausted', self.agent.run('synthetic objective', max_turns=0))
        self.api.messages.create.assert_not_called()

    def test_bridge_failure_stops_before_model_call(self):
        with patch.object(self.agent.notebook, 'get_all', side_effect=requests.ConnectionError('offline fixture')):
            with self.assertRaises(requests.ConnectionError):
                self.agent.run('synthetic objective', max_turns=1)
        self.api.messages.create.assert_not_called()

    def test_default_mutation_previews_do_not_commit(self):
        for tool, payload in [
            ('rename_symbol', {'address': '0x1000', 'new_name': 'synthetic_label'}),
            ('set_comment', {'address': '0x1000', 'text': 'synthetic comment'}),
            ('tag_function', {'function': '0x1000', 'tag': 'synthetic'}),
            ('patch_preview', {'address': '0x1000', 'patch': '90'}),
        ]:
            with self.subTest(tool=tool):
                self.assertFalse(self.agent.bridge.dispatch(tool, payload)['committed'])
        self.assertEqual(bridge_mock._renames, {})

    def test_unknown_tool_reports_error(self):
        self.assertIn('error', self.agent.bridge.dispatch('unknown_tool', {}))


if __name__ == '__main__':
    unittest.main()
