from notiontool.harness_account import runnable_accounts


class Store:
    def __init__(self, accounts, selected=''):
        self.accounts, self.selected = accounts, selected

    def all(self, kind):
        return list(self.accounts) if kind == 'accounts' else []

    def settings(self):
        return {'harness_account_id': self.selected}


def test_every_ready_account_runs_not_only_the_selected_one():
    store = Store([{'id': 'slot2', 'state': 'ready'}, {'id': 'slot1', 'state': 'ready'},
                   {'id': 'slot4', 'state': 'ai_disabled'}], selected='slot2')
    assert runnable_accounts(store) == {'slot2', 'slot1'}


def test_falls_back_to_selected_when_none_ready():
    store = Store([{'id': 'slot4', 'state': 'ai_disabled'}], selected='slot4')
    assert runnable_accounts(store) == {'slot4'}
    assert runnable_accounts(Store([])) == set()
