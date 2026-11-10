from pathlib import Path

VENDOR = Path(__file__).resolve().parents[1] / "vendor" / "woob"


def test_woob_is_importable_from_vendor():
    import woob

    assert Path(woob.__file__).resolve().is_relative_to(VENDOR)


def test_boursorama_module_is_vendored():
    assert (VENDOR / "modules" / "boursorama" / "module.py").is_file()
