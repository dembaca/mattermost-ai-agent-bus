"""RT-6.1 step 7, RT-6.18 step 7: a postEnd that cannot unregister the bot says why, for each
credential, and names neither (docs/arc42/06-runtime-view.md)."""

from __future__ import annotations

STOP = "adapters/corral/90-mm-session-stop.sh"
TOKEN = "bot-token-for-tests"
SECRET = "reg-secret-for-tests"


def _session(run, sid: str, name: str = "mac-proj-eee555") -> None:
    d = run.state_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.env").write_text(
        f"MM_REG_BOT_NAME={name}\nMM_BOT_USERNAME=agent-{name}\nMM_BOT_TOKEN={TOKEN}\n")


def test_a_failed_unregister_says_why_for_token_and_secret(mm, run):
    _session(run, "sess-why")
    # The token's answer echoes the credential (a proxy echoing headers): not shown.
    mm.unregister_error = {TOKEN: (500, "upstream said: {auth}"), SECRET: (403, "not your bot")}
    r = run(STOP, CORRAL_SESSION_ID="sess-why", MM_BOT_TOKEN=None, MM_REG_SECRET=SECRET)
    assert r.code == 0, r.stderr
    assert ("WARNING: could not unregister mac-proj-eee555: "
            "token: unregister failed http=500: (the answer is not shown: it holds the credential); "
            "secret: unregister failed http=403: not your bot") in r.stderr, r.stderr
    assert "retry: " in r.stderr
    assert TOKEN not in r.stderr and SECRET not in r.stderr
    assert (run.state_dir / "sess-why.env").exists(), "kept for the retry"


def test_without_a_secret_the_reason_says_so(mm, run):
    _session(run, "sess-why2")
    mm.unregister_error = {TOKEN: (500, '{"message": "registrar down"}')}
    r = run(STOP, CORRAL_SESSION_ID="sess-why2", MM_BOT_TOKEN=None, MM_REG_SECRET=None)
    assert ("could not unregister mac-proj-eee555: token: unregister failed http=500: "
            "registrar down; secret: no secret") in r.stderr, r.stderr
    assert TOKEN not in r.stderr


def test_the_secret_unregisters_when_the_token_fails(mm, run):
    _session(run, "sess-why3")
    mm.unregister_error = {TOKEN: (403, "no")}
    r = run(STOP, CORRAL_SESSION_ID="sess-why3", MM_BOT_TOKEN=None, MM_REG_SECRET=SECRET)
    assert "unregistered mac-proj-eee555" in r.stderr and "WARNING" not in r.stderr, r.stderr
    assert not (run.state_dir / "sess-why3.env").exists()
