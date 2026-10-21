"""#179: adaptive fetch window — CPU-pressure ladder with an env ceiling;
zero-regression by default (cap = base = today's behavior)."""



from confluence_knowledge import jobs, server
from confluence_knowledge.index import Index
from confluence_knowledge.jobs import Progress, crawl_control


def _ctl(tmp_path, monkeypatch, battery=False):
    # force AC by default — this laptop is genuinely on battery and the
    # saver halves the cap independently of the CPU ladder; _battery is
    # latched at ctor so the env must land BEFORE the factory call
    monkeypatch.setenv("KNOWLEDGE_FORCE_BATTERY", "1" if battery else "0")
    idx = Index(tmp_path / "s.db")
    idx.create_job("j", "1")
    idx.save_job("j", "running", ["page:1"], [])
    idx.acquire_lease("j", server._owner.value, server._owner.pid)
    return crawl_control(idx, "j", Progress(), cancel_flag=None,
                         owner=server._owner, hb_s=60)


def _cpu(monkeypatch, pct):
    monkeypatch.setattr(jobs, "_cpu_percent", lambda: pct)
    monkeypatch.setattr(jobs, "_CPU_SAMPLE_S", 0.0)   # sample every call


def test_default_is_inert(tmp_path, monkeypatch):
    _cpu(monkeypatch, 10.0)                    # idle CPU
    monkeypatch.delenv("KNOWLEDGE_FETCH_MAX", raising=False)
    ctl = _ctl(tmp_path, monkeypatch)
    assert ctl.fetch_concurrency(6) == 6       # cap = base → no scale-up
    assert ctl.fetch_ceiling(6) == 6


def test_scales_down_under_pressure(tmp_path, monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_FETCH_MAX", raising=False)
    ctl = _ctl(tmp_path, monkeypatch)
    _cpu(monkeypatch, 80.0)
    assert ctl.fetch_concurrency(6) == 3
    _cpu(monkeypatch, 95.0)
    assert ctl.fetch_concurrency(6) == 1
    _cpu(monkeypatch, 50.0)
    assert ctl.fetch_concurrency(6) == 6       # recovers


def test_scales_up_to_env_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_FETCH_MAX", "12")
    ctl = _ctl(tmp_path, monkeypatch)
    _cpu(monkeypatch, 15.0)
    assert ctl.fetch_concurrency(6) == 9       # 1.5x band, capped at 12
    assert ctl.fetch_ceiling(6) == 12          # pool sized for it


def test_battery_still_halves(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_FETCH_MAX", "12")
    ctl = _ctl(tmp_path, monkeypatch, battery=True)
    _cpu(monkeypatch, 15.0)                    # idle CPU, but on battery
    assert ctl.fetch_concurrency(6) == 3       # battery floor wins
