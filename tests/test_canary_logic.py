"""Logica del canary YouTube, verificata OFFLINE (il canary vero gira solo con
GRABMEDIA_CANARY=1). Importa ``evaluate`` dal modulo del canary: i suoi test
restano saltati, la funzione è pura."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests import test_canary_youtube as canary

GOOD_VERBOSE = [
    "[debug] Command-line config: []",
    "[debug] yt-dlp version 2026.10.26",
    "[debug] JS runtimes: deno-2.5.6",
    "[debug] [youtube] Extracting URL: https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "[debug] [youtube] [jsc:deno] Solving JS challenges using deno",
    "[debug] [youtube] dQw4w9WgXcQ: Downloading player",
    "[info] Available formats for dQw4w9WgXcQ",
]
GOOD_IDS = ["251", "251"]
WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "bump-ytdlp.yml"


def test_canary_passes_with_runtime_and_clean_verbose():
    verdict = canary.evaluate(GOOD_VERBOSE, True, [128.9, 126.6], GOOD_IDS)
    assert verdict.ok and verdict.reasons == []
    assert verdict.median_abr == pytest.approx(127.75) and verdict.runtime_line == "deno-2.5.6"


# ------------------------------------------------------ gate primario: signature
@pytest.mark.parametrize("fallback_line", [
    "[debug] [youtube] dQw4w9WgXcQ: nsig extraction failed: Some formats may be missing",
    "WARNING: [youtube] dQw4w9WgXcQ: Signature solving failed, falling back to the legacy solver",
    "[debug] [youtube] [jsc] unable to solve n challenge: falling back to formats without signature",
    "WARNING: [youtube] Falling back: nsig solver timed out",
])
def test_fake_verbose_with_signature_fallback_fails_canary(fallback_line: str):
    """Verbose finto con la riga di fallback: il canary deve fallire anche con
    runtime rilevato, nessun warning sul runtime, formato e bitrate a posto."""
    verdict = canary.evaluate(GOOD_VERBOSE + [fallback_line], True, [128.9, 126.6], GOOD_IDS)
    assert not verdict.ok
    assert any("fallback sulla risoluzione della signature" in r for r in verdict.reasons)
    assert len(verdict.reasons) == 1  # fallisce SOLO per quella ragione
    assert verdict.reasons[0].startswith("fallback")  # ed è la prima: gate primario


# ------------------------------------------------------ asserzione runtime
def test_runtime_assertions_core_ytdlp_line_and_warning():
    # il core non rileva il runtime
    verdict = canary.evaluate(GOOD_VERBOSE, False, [128.9, 126.6], GOOD_IDS)
    assert not verdict.ok and any("nessun runtime JavaScript rilevato" in r for r in verdict.reasons)
    # yt-dlp dice "none"
    none_lines = [ln.replace("deno-2.5.6", "none") for ln in GOOD_VERBOSE]
    verdict = canary.evaluate(none_lines, True, [128.9, 126.6], GOOD_IDS)
    assert not verdict.ok and verdict.runtime_line == "none"
    assert any("yt-dlp non rileva nessun runtime JS" in r for r in verdict.reasons)
    # "none (disabled)" vale come none
    disabled = [ln.replace("deno-2.5.6", "none (disabled)") for ln in GOOD_VERBOSE]
    assert not canary.evaluate(disabled, True, [128.9, 126.6], GOOD_IDS).ok
    # la riga di rilevamento manca del tutto
    missing = [ln for ln in GOOD_VERBOSE if "JS runtimes" not in ln]
    verdict = canary.evaluate(missing, True, [128.9, 126.6], GOOD_IDS)
    assert not verdict.ok and any("'JS runtimes:' assente" in r for r in verdict.reasons)
    # warning esplicito di yt-dlp
    warned = GOOD_VERBOSE + ["WARNING: [youtube] No supported JavaScript runtime could be found. Only deno is enabled by default"]
    verdict = canary.evaluate(warned, True, [128.9, 126.6], GOOD_IDS)
    assert not verdict.ok and any("runtime JS assente" in r for r in verdict.reasons)
    # node al posto di deno va bene
    node = [ln.replace("deno-2.5.6", "node-22.11.0") for ln in GOOD_VERBOSE]
    assert canary.evaluate(node, True, [128.9, 126.6], GOOD_IDS).ok


# ------------------------------------------------------ id formato ammesso
def test_allowed_format_ids_are_251_or_140():
    assert canary.ALLOWED_FORMAT_IDS == frozenset({"251", "140"})
    assert canary.evaluate(GOOD_VERBOSE, True, [128.9, 129.5], ["251", "140"]).ok
    for bad in ("250", "249", "18", "251-drc", None):
        verdict = canary.evaluate(GOOD_VERBOSE, True, [128.9, 126.6], ["251", bad])
        assert not verdict.ok, bad
        assert any("formato inatteso" in r and "ammessi: 140, 251" in r for r in verdict.reasons), bad


# ------------------------------------------------------ pavimento di bitrate
def test_bitrate_floor_is_100_and_only_a_floor():
    assert canary.MIN_MEDIAN_ABR_KBPS == 100.0
    # i valori reali di opus 251 (anche senza runtime) passano: il bitrate non è il gate
    assert canary.evaluate(GOOD_VERBOSE, True, [128.9, 126.6], GOOD_IDS).ok
    assert canary.evaluate(GOOD_VERBOSE, True, [106.0, 129.0], GOOD_IDS).ok      # mediana 117.5
    assert canary.evaluate(GOOD_VERBOSE, True, [100.1, 100.1], GOOD_IDS).ok
    for values in ([100.0, 100.0], [90.0, 105.0], [50.0, 65.0]):                 # mediana <= 100
        verdict = canary.evaluate(GOOD_VERBOSE, True, values, GOOD_IDS)
        assert not verdict.ok and any("pavimento 100" in r for r in verdict.reasons), values
    verdict = canary.evaluate(GOOD_VERBOSE, True, [None, 160.0], GOOD_IDS)
    assert not verdict.ok and any("bitrate non riportato" in r for r in verdict.reasons)
    assert not canary.evaluate(GOOD_VERBOSE, True, [None, None], GOOD_IDS).ok
    assert len(canary.CANARY_VIDEOS) == 2 and all(len(v) == 11 for v in canary.CANARY_VIDEOS)
    # il commento dichiara che è un pavimento e che il gate primario è la regex di fallback
    source = Path(canary.__file__).read_text(encoding="utf-8")
    assert "PAVIMENTO" in source and "non una misura di qualità" in source and "gate" in source
    assert "SIGNATURE_FALLBACK_RE" in source.split("MIN_MEDIAN_ABR_KBPS = 100.0")[0][-400:]


def test_all_failure_reasons_are_collected():
    lines = [ln.replace("deno-2.5.6", "none") for ln in GOOD_VERBOSE] + ["[debug] nsig extraction failed"]
    verdict = canary.evaluate(lines, False, [50.0, 60.0], ["249", "250"])
    assert len(verdict.reasons) == 5 and verdict.reasons[0].startswith("fallback")


def test_canary_is_skippable_locally_without_env(monkeypatch):
    monkeypatch.delenv("GRABMEDIA_CANARY", raising=False)
    mark = canary.pytestmark
    assert mark.name == "skipif" and mark.args[0] is True  # valutato all'import senza variabile


# ------------------------------------------------------ workflow
def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_is_valid_yaml_with_expected_shape():
    """Il file deve essere YAML valido: uno scalare con ': ' non quotato lo rompe
    in silenzio (è successo) e GitHub rifiuterebbe l'intero workflow."""
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_workflow())
    triggers = data.get("on", data.get(True))  # YAML 1.1 legge la chiave "on" come booleano
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    assert data["permissions"] == {"contents": "write", "pull-requests": "write", "issues": "write"}
    assert data["concurrency"] == {"group": "bump-ytdlp", "cancel-in-progress": True}
    # workflow_dispatch presente, con la versione opzionale e il flag per verificare senza bump
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"version", "force_checks"}
    # un solo job: nessun "needs" da rendere esplicito; il reporting è negli step finali
    assert list(data["jobs"]) == ["bump"] and "needs" not in data["jobs"]["bump"]
    steps = data["jobs"]["bump"]["steps"]
    ids = [s.get("id") for s in steps if s.get("id")]
    assert ids == ["versions", "suite", "runtime", "canary"]
    assert all(("run" in s) != ("uses" in s) for s in steps), "ogni step ha run oppure uses"
    canary_step = next(s for s in steps if s.get("id") == "canary")
    assert canary_step["env"] == {"GRABMEDIA_CANARY": "1"}


def test_reporting_steps_always_run_with_token():
    """Riepilogo e issue girano anche se uno step precedente fallisce in modo
    secco (if: always()), con GH_TOKEN da secrets.GITHUB_TOKEN nel loro env."""
    yaml = pytest.importorskip("yaml")
    steps = yaml.safe_load(_workflow())["jobs"]["bump"]["steps"]
    summary = next(s for s in steps if s.get("name", "").startswith("Riepilogo"))
    issue = next(s for s in steps if s.get("name", "").startswith("Rosso"))
    close = next(s for s in steps if s.get("name", "").startswith("Verde - chiudi"))
    assert summary["if"] == "always()"
    assert "always()" in issue["if"] and "failure()" in issue["if"]
    for outcome in ("suite", "runtime", "canary"):
        assert f"steps.{outcome}.outcome == 'failure'" in issue["if"]
    for step in (summary, issue, close):
        assert step["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    # il riepilogo viene prima della issue, che ne riusa il contenuto; entrambi dopo il canary
    names = [s.get("name", "") for s in steps]
    assert names.index(summary["name"]) < names.index(issue["name"]) == len(names) - 1
    assert names.index(next(n for n in names if n.startswith("Canary - YouTube"))) < names.index(summary["name"])
    # gli step che possono fallire senza fermare il job
    for step_id in ("suite", "runtime", "canary"):
        assert next(s for s in steps if s.get("id") == step_id)["continue-on-error"] is True
    assert 'cat report.md >> "$GITHUB_STEP_SUMMARY"' in summary["run"] and "Run: $RUN_URL" in summary["run"]
    assert "tail -c 6000" in summary["run"]


def test_workflow_permissions_concurrency_and_setup():
    text = _workflow()
    permissions = text.split("permissions:", 1)[1].split("concurrency:", 1)[0]
    granted = dict(re.findall(r"^\s+([a-z-]+):\s*(\w+)\s*$", permissions, re.M))
    assert granted == {"contents": "write", "pull-requests": "write", "issues": "write"}
    assert "concurrency:" in text and "cancel-in-progress: true" in text
    assert "denoland/setup-deno" in text and "ffmpeg -version" in text
    commands = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    assert not any(re.search(r"(^|\s|&&\s*)sudo\s", ln) for ln in commands), "nessun comando sudo nel workflow"


def test_workflow_forces_and_guards_the_canary():
    text = _workflow()
    assert 'GRABMEDIA_CANARY: "1"' in text
    assert "skipped" in text and "no tests ran" in text and "exit 1" in text  # anti-skip


def test_workflow_asserts_js_runtime_before_canary():
    text = _workflow()
    assert "deno --version" in text and "JS runtimes:" in text
    assert re.search(r"JS runtimes:\[\[:space:\]\]\*none", text), "la riga 'JS runtimes: none' deve far fallire lo step"
    assert text.index("deno --version 2>&1") < text.index("tests/test_canary_youtube.py -q"), "asserzione runtime prima del canary"
    # lo step runtime partecipa al verdetto verde/rosso
    assert "steps.runtime.outcome == 'success'" in text and "steps.runtime.outcome == 'failure'" in text


def test_workflow_red_path_dedupes_issue_by_label():
    text = _workflow()
    red = text.split("- name: Rosso", 1)[1]
    assert "CANARY_LABEL: canary" in text and "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in red
    # label creata se assente
    assert "gh label list" in red and 'gh label create "$CANARY_LABEL"' in red
    # deduplica: cerca una issue aperta con la label, commenta se c'è, altrimenti crea
    assert 'gh issue list --label "$CANARY_LABEL" --state open' in red
    assert red.index("gh issue list --label") < red.index("gh issue comment") < red.index("gh issue create")
    assert 'gh issue comment "$existing" --body-file report.md' in red
    assert 'gh issue create --label "$CANARY_LABEL" --body-file report.md' in red
    # il contenuto (log + link al run) è quello del riepilogo; il job fallisce con annotazione
    assert "::error::" in red and red.rstrip().endswith("exit 1")
    assert "actions/runs/${{ github.run_id }}" in text


def test_workflow_green_path_closes_open_canary_issues():
    text = _workflow()
    green = text.split("- name: Verde - chiudi le issue canary aperte", 1)[1].split("- name: Rosso", 1)[0]
    assert "steps.suite.outcome == 'success'" in green and "steps.canary.outcome == 'success'" in green
    assert 'gh issue list --label "$CANARY_LABEL" --state open' in green
    assert 'gh issue close "$number"' in green and "$RUN_URL" in green
