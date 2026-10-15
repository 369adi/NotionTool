import unittest
from notiontool.model_verification import verify_execution,ModelVerificationError


class ModelVerificationTests(unittest.TestCase):
    def turn(self,model='opus-id'):
        return [{'type':'config','value':{'model':'opus-id','modelFromUser':True}},
                {'type':'user','id':'own'},
                {'type':'agent-inference','model':model,'value':[{'notionModelName':model}]}]

    def test_own_actual_opus_response_is_verified(self):
        self.assertEqual(verify_execution(self.turn(),'own','opus-id'),['opus-id'])

    def test_explicit_config_cannot_hide_sonnet_execution(self):
        with self.assertRaises(ModelVerificationError) as caught:
            verify_execution(self.turn('sonnet-low'),'own','opus-id')
        self.assertEqual(caught.exception.reported,['sonnet-low'])

    def test_other_users_turn_and_old_opus_are_not_evidence(self):
        steps=self.turn()+[{'type':'user','id':'different'},
                           {'type':'agent-inference','model':'opus-id','value':[]}]
        with self.assertRaises(ModelVerificationError):verify_execution(steps,'absent','opus-id')
        with self.assertRaises(ModelVerificationError):verify_execution(self.turn('sonnet-low')+steps[3:],'own','opus-id')

    def test_disagreeing_server_fields_are_rejected(self):
        steps=self.turn();steps[-1]['value'][0]['notionModelName']='sonnet-low'
        with self.assertRaises(ModelVerificationError):verify_execution(steps,'own','opus-id')

    def test_missing_execution_fields_do_not_accept_model_claim_in_text(self):
        steps=[{'type':'user','id':'own'},{'type':'agent-inference','value':[['I am Opus 5.5']]}]
        with self.assertRaises(ModelVerificationError) as caught:verify_execution(steps,'own','opus-id')
        self.assertEqual(caught.exception.reason,'missing')

    def test_haiku_choice_is_fresh_per_task(self):
        self.assertEqual(verify_execution(self.turn('haiku-id'),'own','haiku-id'),['haiku-id'])
        with self.assertRaises(ModelVerificationError):verify_execution(self.turn('haiku-id'),'own','opus-id')

    def test_earlier_good_step_does_not_hide_later_wrong_inference(self):
        steps=self.turn()+[{'type':'agent-inference','model':'sonnet-low','value':[]}]
        with self.assertRaises(ModelVerificationError):verify_execution(steps,'own','opus-id')


from notiontool.model_verification import turn_status


class TurnStatusTests(unittest.TestCase):
    def test_server_error_step_is_a_confirmed_failed_turn(self):
        steps=[{'type':'user','id':'own'},{'type':'error','id':'e1','subType':'temporarily-unavailable'}]
        state,error=turn_status(steps,'own',[{'status':'error','final_step_id':'e1'}])
        self.assertEqual((state,error['subType']),('error','temporarily-unavailable'))

    def test_outcome_status_error_without_any_output(self):
        self.assertEqual(turn_status([{'type':'user','id':'own'}],'own',[{'status':'error','final_step_id':'own'}]),('error',None))

    def test_error_after_output_is_never_a_clean_failure(self):
        steps=[{'type':'user','id':'own'},{'type':'agent-inference','id':'a1'},{'type':'error','id':'e1'}]
        self.assertEqual(turn_status(steps,'own',[{'status':'error','final_step_id':'e1'}]),('completed',None))

    def test_turn_without_outcome_is_still_running(self):
        self.assertEqual(turn_status([{'type':'user','id':'own'}],'own',[]),('running',None))

    def test_completed_turn_ignores_the_next_turn_and_missing_step(self):
        steps=[{'type':'user','id':'own'},{'type':'agent-inference','id':'a1'},
               {'type':'user','id':'next'},{'type':'error','id':'e2'}]
        self.assertEqual(turn_status(steps,'own',[{'status':'completed','final_step_id':'a1'}]),('completed',None))
        self.assertEqual(turn_status(steps,'absent',[]),('missing',None))
