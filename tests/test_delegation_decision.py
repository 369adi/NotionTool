from notiontool.parallel_plan import delegation_decision


def parts(n, s, files=None):
    return [dict(id=str(i), est_s=s, files=[files] if files else ['f%d' % i]) for i in range(n)]


def test_tiny_parts_stay_solo():
    # bench B: 6 parts of ~5 s each, the Main alone was faster (31 s vs ~69 s)
    d = delegation_decision(parts(6, 5), free_young=6)
    assert d['delegate'] is False and d['count'] == 0


def test_big_parts_delegate_wide():
    d = delegation_decision(parts(5, 300), free_young=10)
    assert d['delegate'] is True and d['count'] == 5
    assert d['parallel_s'] < 500 < d['solo_s']


def test_same_file_parts_do_not_parallelise():
    d = delegation_decision(parts(4, 300, files='same.py'), free_young=10)
    assert d['delegate'] is False


def test_single_part_or_one_coworker_is_solo():
    assert delegation_decision(parts(1, 900))['delegate'] is False
    assert delegation_decision(parts(4, 300), free_young=1)['delegate'] is False


def test_break_even_two_parts():
    assert delegation_decision(parts(2, 60), free_young=2)['delegate'] is False
    assert delegation_decision(parts(2, 200), free_young=2)['delegate'] is True
