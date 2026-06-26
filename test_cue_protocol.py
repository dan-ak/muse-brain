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


from cue_protocol import CueSession, SessionState


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def _one_cue_seq():
    # rest(5) -> focus(10) -> rest(5); cumulative ends = [5, 15, 20]
    return [Trial(REST, 5.0), Trial(FOCUS, 10.0, "right"), Trial(REST, 5.0)]


def test_session_reports_leading_rest():
    clk = FakeClock(0.0)
    s = CueSession(_one_cue_seq(), clock_fn=clk)
    st = s.update(0.0)
    assert isinstance(st, SessionState)
    assert st.phase == REST
    assert st.done is False
    assert st.time_remaining == 5.0


def test_session_reports_active_cue_and_countdown():
    clk = FakeClock(0.0)
    s = CueSession(_one_cue_seq(), clock_fn=clk)
    s.update(0.0)
    clk.t = 7.0
    st = s.update(0.0)
    assert st.phase == FOCUS
    assert st.goal_side == "right"
    assert st.time_remaining == 8.0   # ends[1]=15 - elapsed 7
    assert st.cue_number == 1


def test_session_marks_done_after_last_trial():
    clk = FakeClock(0.0)
    s = CueSession(_one_cue_seq(), clock_fn=clk)
    s.update(0.0)
    clk.t = 20.0
    st = s.update(0.0)
    assert st.done is True
