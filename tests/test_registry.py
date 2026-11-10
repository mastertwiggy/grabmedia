import pytest

from core import registry
from sources.base import ProviderError, ProviderUnavailable


def test_available_modules_lists_disk_without_importing(fake_sources):
    names = registry.available_modules()
    assert {"fakeok", "fakebroken", "fakeraises", "fakemeta", "fakeflaky", "fakegreedy"} <= set(names)
    assert "base" not in names
    assert registry.REGISTRY == {}  # niente importato finché non serve


def test_load_is_lazy_and_cached(fake_sources):
    src = registry.load("fakeok")
    assert src is not None and src.name == "fakeok"
    assert list(registry.REGISTRY) == ["fakeok"]
    assert registry.load("fakeok") is src


def test_broken_providers_do_not_block_startup(fake_sources):
    loaded = registry.load_all([])
    names = [s.name for s in loaded]
    assert "fakeok" in names and "fakemeta" in names
    assert "fakebroken" not in names and "fakeraises" not in names
    assert "dipendenza mancante 'this_dependency_does_not_exist'" in registry.UNAVAILABLE["fakebroken"]
    assert "RuntimeError: bug nel modulo provider" == registry.UNAVAILABLE["fakeraises"]


def test_load_unknown_module(fake_sources):
    assert registry.load("nonexistent") is None
    assert "nessun modulo sources/nonexistent.py" in registry.UNAVAILABLE["nonexistent"]


def test_load_all_respects_priority_order(fake_sources):
    loaded = registry.load_all(["fakemeta", "fakeok"])
    names = [s.name for s in loaded]
    assert names[:2] == ["fakemeta", "fakeok"]
    assert names[2:] == sorted(names[2:])


def test_dispatch_uses_priority_and_fallback_last(fake_sources):
    # fakeok e fakemeta rivendicano host diversi: vince chi è primo nell'ordine solo a parità
    assert registry.dispatch("https://fakeok.test/x", order=["fakeok", "fakegreedy"]).name == "fakeok"
    registry.reset()
    # fakegreedy accetta tutto ma è fallback: non batte mai un provider primario, anche se primo
    assert registry.dispatch("https://fakeok.test/x", order=["fakegreedy", "fakeok"]).name == "fakeok"
    assert registry.dispatch("https://nobody.example/x", order=["fakeok"]).name == "fakegreedy"


def test_dispatch_reports_unavailable_claimant_instead_of_fallback(fake_sources):
    with pytest.raises(ProviderUnavailable) as exc:
        registry.dispatch("https://fakeunavail.test/x", order=["fakeok", "fakegreedy"])
    text = str(exc.value)
    assert "fakeunavail" in text and "credenziali mancanti" in text and "--provider" in text
    assert "fakeunavail" in registry.UNAVAILABLE_CLASSES
    # gli altri URL continuano ad andare al fallback
    assert registry.dispatch("https://nobody.example/x").name == "fakegreedy"


def test_dispatch_forced(fake_sources):
    assert registry.dispatch("https://whatever", forced="fakeok").name == "fakeok"
    with pytest.raises(ProviderUnavailable) as exc:
        registry.dispatch("https://whatever", forced="fakebroken")
    assert "fakebroken" in str(exc.value) and "--provider" in str(exc.value)
    with pytest.raises(ProviderUnavailable):
        registry.dispatch("https://whatever", forced="nope")


def test_dispatch_no_match_is_actionable(fake_sources, monkeypatch):
    # Solo provider finti (quelli reali dipendono da ffmpeg nel PATH) e senza il fallback
    monkeypatch.setattr(registry, "available_modules", lambda: ["fakebroken", "fakeok", "fakemeta"])
    with pytest.raises(ProviderError) as exc:
        registry.dispatch("https://unknown.example/x", order=["fakeok"])
    text = str(exc.value)
    assert "Nessun provider" in text and "fakeok" in text and "fakebroken" in text


def test_audio_provider_delegation(fake_sources):
    meta = registry.load("fakemeta")
    assert meta is not None and not meta.supports_direct_audio
    target = registry.audio_provider_for(meta)
    assert target.name == "fakeok" and target.supports_direct_audio
    ok = registry.load("fakeok")
    assert registry.audio_provider_for(ok) is ok


def test_audio_provider_missing_is_actionable(fake_sources):
    meta = registry.load("fakemeta")
    meta.audio_provider = "fakebroken"
    with pytest.raises(ProviderUnavailable) as exc:
        registry.audio_provider_for(meta)
    assert "fakebroken" in str(exc.value)
    meta.audio_provider = "fakemeta"
    with pytest.raises(ProviderError, match="non scarica audio"):
        registry.audio_provider_for(meta)


def test_metadata_source_download_delegates(fake_sources, tmp_path):
    from sources.base import MetadataSource, Track

    class M(MetadataSource):
        name = "m"

        def can_handle(self, url):
            return True

        def resolve(self, url):
            return []

    m = M()
    m.audio_provider = "fakeok"
    out = m.download(Track("m", "1", "t", ["a"]), tmp_path / "t", "best")
    assert out.exists() and out.suffix == ".ogg"
    m.audio_provider = "fakebroken"
    with pytest.raises(ProviderUnavailable):
        m.download(Track("m", "1", "t"), tmp_path / "t", "best")
