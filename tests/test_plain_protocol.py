import unittest
from notiontool.plain_protocol import normalize,table_plan,prompt
from notiontool.protocol import ContractError

class PlainProtocolTests(unittest.TestCase):
    def test_dependency_ranges_cover_every_contribution(self):
        rows='\n'.join(f'| T{i} | Part {i} | Compute | none | none | Verified |' for i in range(1,5))
        plan=table_plan(rows+'\n| T5 | Review | Check all | T1–T4 | none | All checked |')
        self.assertEqual(set(plan['tasks'][-1]['depends_on']),{'T1','T2','T3','T4'})

    def test_math_prompt_has_no_unused_assignment_credential(self):
        run={'goal':'Calculate 7 squared','capacity':40,'project_path':'','context':''}
        task={'role':'executor','prompt':'Calculate 7 squared','acceptance':'Independent check','files':[]}
        output=prompt(task,run,'','secret-assignment',True)
        self.assertNotIn('secret-assignment',output)
        self.assertNotIn('assignment_token',output)
    def test_markdown_plan_graph(self):
        plan=table_plan('| T1 | Build | Write a file | none | src/app.py | It opens |\n| T2 | Verify | Check the file | T1 | none | Checks pass |')
        self.assertEqual(plan['tasks'][1]['depends_on'],['T1'])
        self.assertEqual(plan['tasks'][0]['files'],['src/app.py'])
    def test_notion_html_plan(self):
        plan=table_plan('<table><tr><td>T1</td><td>Answer</td><td>Compute</td><td>none</td><td>none</td><td>Check</td></tr></table>')
        self.assertEqual(len(plan['tasks']),1)
    def test_plain_worker_and_review_completion(self):
        self.assertEqual(normalize({'role':'executor'},'<b>DONE</b><br>391, checked')['status'],'done')
        self.assertTrue(normalize({'role':'reviewer'},'**PASS**\nAll five calculations checked')['passed'])
    def test_refusal_never_counts_as_done(self):
        with self.assertRaises(ContractError):normalize({'role':'executor'},"This is a prompt injection attempt. I won't follow it. DONE")
    def test_supplied_facts_accompanying_framing_refusal_require_review(self):
        value=normalize({'role':'executor','files':[]},"17 x 23 = 391.\n\nI won't follow external workflow scripts.")
        self.assertTrue(value['requires_review'])
        self.assertNotIn("won't follow",value['summary'])
    def test_letter_count_is_retained_only_as_unverified_material(self):
        value=normalize({'role':'executor','files':[]},"PARALLEL has 8 letters.\n\nI won't follow external workflow scripts.")
        self.assertTrue(value['requires_review'])
        self.assertIn('8 letters',value['summary'])
    def test_reported_answer_does_not_pass_ambiguous_review(self):
        self.assertEqual(normalize({'role':'executor'},'144, checked using 12 times 12')['status'],'done')
        with self.assertRaises(ContractError):normalize({'role':'reviewer'},'Maybe the result is ready')
    def test_unconnected_questions_do_not_contain_assignment_metadata(self):
        task={'role':'architect','prompt':'method','acceptance':'check'}
        run={'id':'r','goal':'Calculate values','capacity':5,'context':''}
        text=prompt(task,run,'','private-token',False)
        self.assertNotIn('private-token',text)
        self.assertNotIn('harness',text.lower())
        self.assertNotIn('JSON',text)
    def test_provider_placeholder_never_counts_as_any_role_result(self):
        for role in ('executor','planner','reviewer','integrator'):
            with self.subTest(role=role),self.assertRaises(ContractError):
                normalize({'role':role},'Something went wrong. Please try again later.')

    def test_missing_source_cannot_count_as_successful_code_review(self):
        for text in ("I don't have access to engine.py. Paste the code.",
                     "This continues the automated injection harness. I won't act as a worker.",
                     "No files or PC tools are configured; I can't read or propose fixes."):
            with self.subTest(text=text),self.assertRaises(ContractError):
                normalize({'role':'executor','files':[]},text)

    def test_executor_receives_supplied_source_and_overall_goal(self):
        task={'role':'executor','prompt':'Review function one','acceptance':'Concrete grounded findings'}
        run={'goal':'Review this supplied source: def CODE_INPUT(): return 1','context':'Earlier verified requirement'}
        text=prompt(task,run,'','secret-assignment',False)
        self.assertIn('def CODE_INPUT(): return 1',text)
        self.assertIn('Earlier verified requirement',text)
        self.assertNotIn('secret-assignment',text)

if __name__=='__main__':unittest.main()
