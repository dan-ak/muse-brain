from cue_protocol import Trial, build_sequence, FOCUS, RELAX, REST


def test_sequence_brackets_each_cue_with_rest():
    seq = build_sequence(n_cues=4, seed=1, rest_s=5.0, cue_s=10.0)
    kinds = [t.kind for t in seq]
    assert kinds[0] == REST and kinds[-1] == REST   # leading + trailing rest
    assert len(seq) == 2 * 4 + 1                     # rest before each cue + trailing


def test_sequence_is_balanced():
    seq = build_sequence(n_cues=4, seed=1)
    cues = [t.kind for t in seq if t.kind in (FOCUS, RELAX)]
    assert len(cues) == 4
    assert cues.count(FOCUS) == 2
    assert cues.count(RELAX) == 2


def test_sequence_is_seed_deterministic():
    a = [t.kind for t in build_sequence(n_cues=6, seed=7)]
    b = [t.kind for t in build_sequence(n_cues=6, seed=7)]
    assert a == b


def test_cue_trials_carry_goal_side():
    seq = build_sequence(n_cues=2, seed=0)
    for t in seq:
        if t.kind == FOCUS:
            assert t.goal_side == "right"
        elif t.kind == RELAX:
            assert t.goal_side == "left"
        else:
            assert t.goal_side is None


def test_odd_n_cues_rounded_down_to_balance():
    seq = build_sequence(n_cues=5, seed=0)
    cues = [t.kind for t in seq if t.kind in (FOCUS, RELAX)]
    assert len(cues) == 4   # 5 // 2 * 2
