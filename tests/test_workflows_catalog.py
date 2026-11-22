import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_workflows import BaseWorkspace
from agentctl.common import UserError
from agentctl.team import validate_plan, run_team
from agentctl.workflows import catalog, compile_workflow
from agentctl.project import inventory


class WorkflowCatalogTests(BaseWorkspace):
    def test_all_nine_templates_form_valid_bounded_graphs(self):
        self.assertEqual(len(catalog()), 9)
        for item in catalog():
            plan = compile_workflow(item['name'], 'Check the supplied project with source evidence.')
            validate_plan(plan)
            self.assertTrue(plan['human_review_required'])
            self.assertTrue(all('Do not send messages' in j['goal'] for j in plan['jobs']))

    def test_backtest_template_reuses_framework_and_preserves_protocol(self):
        plan = compile_workflow('backtest-research', 'Evaluate the supplied strategy on development data')
        self.assertEqual([j['id'] for j in plan['jobs']], ['experiment-design', 'experiment-run', 'experiment-review'])
        writer = plan['jobs'][1]
        self.assertEqual(writer['write_scope'], [plan['output_dir'] + '/**'])
        self.assertIn('EXISTING backtest/optimizer interface', writer['goal'])
        self.assertIn('Preserve evaluator code', writer['goal'])
        self.assertIn('Final holdout evaluation is outside', writer['goal'])

    def test_readonly_workflows_have_no_writers(self):
        for name in ('code-review', 'release-check'):
            self.assertTrue(all(j['permission'] == 'read-only' for j in compile_workflow(name, 'Review')['jobs']))

    def test_client_outputs_have_unique_directory_and_scoped_writer(self):
        first = compile_workflow('client-report', 'Report on provided production data')
        second = compile_workflow('client-report', 'Report on provided production data')
        self.assertNotEqual(first['output_dir'], second['output_dir'])
        writer = next(j for j in first['jobs'] if j['permission'] == 'workspace-write')
        self.assertEqual(writer['write_scope'], [first['output_dir'] + '/**'])
        self.assertIn('DRAFT FOR REVIEW', writer['goal'])

    def test_output_path_cannot_escape_or_target_internal_state(self):
        for bad in ('../escape', '/tmp/out', '.agentctl', 'foo\nbar'):
            with self.assertRaises(UserError):
                compile_workflow('client-report', 'Report', output_dir=bad)

    def test_local_option_keeps_numerical_checks_and_final_review_on_codex(self):
        for name in ('client-report', 'bom-audit', 'quote-draft'):
            plan = compile_workflow(name, 'Check inputs', local_readers=True)
            self.assertTrue(all(j['backend'] == 'codex' for j in plan['jobs']))
        docs = compile_workflow('docs', 'Update readme', local_readers=True)
        self.assertEqual(docs['jobs'][0]['backend'], 'local')
        self.assertEqual(docs['jobs'][-1]['backend'], 'codex')

    def test_cli_dry_run_exports_but_does_not_launch(self):
        with patch('agentctl.cli.run_team', side_effect=AssertionError('must not run')):
            rc, output, err = self.cli('workflow', 'run', 'bugfix', '--goal', 'Fix CSV output', '--dry-run')
        self.assertEqual(rc, 0, err)
        self.assertIn('No models launched', output)
        plans = list((self.root / '.agentctl/plans').glob('*-bugfix.json'))
        self.assertEqual(len(plans), 1)
        self.assertEqual(json.loads(plans[0].read_text())['workflow'], 'bugfix')

    def test_small_csv_and_tsv_can_be_selected_as_evidence(self):
        (self.root / 'parts.csv').write_text('part,qty\nA,2\n')
        (self.root / 'parts.tsv').write_text('part\tqty\nA\t2\n')
        names = [f['path'] for f in inventory(self.root, self.cfg)['files']]
        self.assertIn('parts.csv', names)
        self.assertIn('parts.tsv', names)

    def test_real_scheduler_executes_compiled_readonly_workflow_with_fixture(self):
        with redirect_stdout(io.StringIO()):
            result = run_team(self.root, self.cfg, compile_workflow('release-check', 'Review CSV implementation'))
        self.assertTrue(result['successful'])
        self.assertEqual(set(result['runs']), {'test-plan', 'doc-audit', 'review'})
