from oc_maintainer.state import RERUN_COOLDOWN_S, State


def _state(tmp_path, now=1000.0):
    return State(str(tmp_path / "s.db"), now)


def test_open_pr_cap(tmp_path):
    st = _state(tmp_path)
    st.touch_miner("hk", "alice")
    assert st.admit("hk", "oc-router")[0]
    st.enqueue("oc-router#1", "oc-router", 1, "hk", "alice", "sha")
    st.set_status("oc-router#1", "running")
    ok, reason = st.admit("hk", "oc-router")
    assert not ok and "in flight" in reason


def test_rerun_cooldown(tmp_path):
    st = _state(tmp_path, now=1000.0)
    st.touch_miner("hk", "alice")
    st.enqueue("oc-router#1", "oc-router", 1, "hk", "alice", "s")
    st.set_status("oc-router#1", "closed")
    assert not st.admit("hk", "oc-router")[0]  # just decided → cooldown
    st.now = 1000.0 + RERUN_COOLDOWN_S + 1
    assert st.admit("hk", "oc-router")[0]


def test_credibility_decay_bans(tmp_path):
    st = _state(tmp_path)
    st.touch_miner("hk", "alice")
    for _ in range(3):
        st.adjust_credibility("hk", -0.34)
    assert st.is_banned("hk")
    assert not st.admit("hk", "oc-router")[0]


def test_metrics_and_queue(tmp_path):
    st = _state(tmp_path)
    st.touch_miner("hk", "alice")
    st.enqueue("oc-router#1", "oc-router", 1, "hk", "alice", "s")
    assert st.metrics()["queue_depth"] == 1
    assert st.queue()[0]["position"] == 1
