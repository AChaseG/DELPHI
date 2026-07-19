"""Language detection drives auto-translation and the language filter."""
import pytest

from backend.app import langdetect


@pytest.mark.parametrize("text,want", [
    ("東京で大きな地震が発生し、津波警報が発令された。", "ja"),
    ("中国政府今天宣布了新的经济政策以促进国家发展和就业", "zh"),
    ("정부는 오늘 새로운 정책을 발표했습니다 오늘 국민에게", "ko"),
    ("Президент подписал новый закон об экономике страны сегодня", "ru"),
    ("Уряд України ухвалив нове рішення щодо оборони країни", "uk"),
    ("Η κυβέρνηση ανακοίνωσε νέα μέτρα για την οικονομία σήμερα", "el"),
    ("أعلنت الحكومة عن إجراءات جديدة بشأن الاقتصاد اليوم", "ar"),
    ("Le gouvernement français a annoncé de nouvelles mesures pour l'économie", "fr"),
    ("Die Regierung hat heute neue Maßnahmen für die Wirtschaft angekündigt", "de"),
    ("El gobierno anunció nuevas medidas para la economía del país hoy", "es"),
    ("O governo anunciou novas medidas para a economia do país hoje", "pt"),
    ("The government announced new measures for the national economy today", "en"),
])
def test_detects_language(text, want):
    assert langdetect.detect(text, "en") == want


def test_weak_signal_keeps_source_tag():
    assert langdetect.detect("OK", "de") == "de"
    assert langdetect.detect("2024 update", "it") == "it"
    assert langdetect.detect("", "ja") == "ja"
