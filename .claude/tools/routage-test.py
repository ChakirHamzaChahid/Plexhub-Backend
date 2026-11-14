#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests de .claude/tools/routage.py (2026-11-14).

    python .claude/tools/routage-test.py
"""
import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

spec = importlib.util.spec_from_file_location("routage", Path(__file__).with_name("routage.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def couple(s, **kw):
    m, e, _, _ = R.route(R.parse(s), **kw)
    return m, e


def check(text):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = R.check(text.splitlines())
    return rc, buf.getvalue()


class GrilleTest(unittest.TestCase):
    # Exemples de la skill model-effort-routing §3
    def test_exemples_de_la_skill(self):
        cas = {
            "C1=0 C2=1 C3=0 C4=0 C5=0": ("sonnet", "medium"),   # cle strings.xml
            "C1=0 C2=0 C3=0 C4=0 C5=0": ("haiku", "medium"),    # renommer une constante
            "C1=1 C2=0 C3=0 C4=0 C5=1": ("sonnet", "medium"),   # use case + tests
            "C1=1 C2=1 C3=1 C4=2 C5=1": ("sonnet", "high"),     # ecran Compose, focus
            "C1=2 C2=2 C3=0 C4=1 C5=1": ("opus", "medium"),     # migration, spec claire (E10)
            "C1=2 C2=2 C3=1 C4=1 C5=1": ("opus", "high"),       # migration avec choix
            "C1=2 C2=2 C3=2 C4=2 C5=2": ("opus", "high"),       # crash player
        }
        for s, attendu in cas.items():
            self.assertEqual(couple(s), attendu, s)

    def test_lecteurs(self):
        self.assertEqual(couple("C1=1 C2=2 C3=1 C4=2 C5=2", lecteur=True), ("sonnet", "high"))
        self.assertEqual(couple("C1=1 C2=1 C3=0 C4=2 C5=2", lecteur=True), ("sonnet", "medium"))
        self.assertEqual(couple("C1=1 C2=2 C3=1 C4=1 C5=2", securite=True), ("opus", "medium"))
        self.assertEqual(couple("C1=1 C2=2 C3=2 C4=1 C5=2", securite=True), ("opus", "high"))

    def test_lecture_seule(self):
        self.assertEqual(couple("C1=2 C2=0 C3=0 C4=0 C5=0", lecture_seule=True), ("haiku", "session"))
        self.assertEqual(couple("C1=2 C2=0 C3=1 C4=0 C5=0", lecture_seule=True, large=True), ("sonnet", "session"))

    def test_notes_variantes(self):
        _, _, _, notes = R.route(R.parse("C1=0 C2=0 C3=0 C4=0 C5=1"))
        self.assertTrue(any("minuscule" in n for n in notes))
        _, _, _, notes = R.route(R.parse("C1=1 C2=1 C3=0 C4=0 C5=0"))
        self.assertTrue(any("opus · low" in n for n in notes))

    def test_notes_manquantes(self):
        with self.assertRaises(ValueError):
            R.parse("C1=1 C2=1")


class CheckTest(unittest.TestCase):
    def test_plan_scr4_deux_ecarts(self):
        rc, out = check(
            "ROUTAGE T2 : model=sonnet effort=medium agent=android-developer — C1=1 C2=1 C3=1 C4=2 C5=1 (focus TV, i18n)\n"
            "ROUTAGE PERF : model=opus effort=medium agent=perf-benchmarker — C1=1 C2=2 C3=1 C4=2 C5=1 (benchmark)\n"
            "ROUTAGE T0a : model=opus effort=medium agent=android-developer — C1=2 C2=2 C3=0 C4=1 C5=1 (Room)\n")
        self.assertEqual(rc, 1)
        self.assertIn("2 ecart(s)", out)

    def test_conforme(self):
        rc, _ = check("ROUTAGE T2 : model=sonnet effort=high agent=android-developer-high — C1=1 C2=1 C3=1 C4=2 C5=1 (x)")
        self.assertEqual(rc, 0)

    def test_suffixe_incoherent(self):
        rc, _ = check("ROUTAGE T2 : model=sonnet effort=high agent=android-developer — C1=1 C2=1 C3=1 C4=2 C5=1 (x)")
        self.assertEqual(rc, 1)

    def test_pilote_low_permis(self):
        rc, _ = check("ROUTAGE T5 : model=sonnet effort=low agent=android-developer-low — C1=1 C2=0 C3=0 C4=0 C5=1 (x)")
        self.assertEqual(rc, 0)

    def test_pilote_low_interdit_en_zone_risque(self):
        rc, _ = check("ROUTAGE T5 : model=opus effort=low agent=android-developer-low — C1=1 C2=2 C3=0 C4=0 C5=1 (x)")
        self.assertEqual(rc, 1)

    def test_ticket_minuscule(self):
        self.assertEqual(check("ROUTAGE T6 : model=session effort=session agent=session — C1=0 C2=0 C3=0 C4=0 C5=1 (x)")[0], 0)
        self.assertEqual(check("ROUTAGE T6 : model=session effort=session agent=session — C1=1 C2=0 C3=0 C4=0 C5=1 (x)")[0], 1)

    def test_escalade_non_verifiee(self):
        rc, out = check("ROUTAGE T2 tentative 2 : model=opus effort=high agent=android-developer-high — C1=1 C2=1 C3=0 C4=0 C5=1 (x)")
        self.assertEqual(rc, 0)
        self.assertIn("escalade", out)

    def test_description_securite_ne_rend_pas_lecteur(self):
        rc, _ = check("ROUTAGE T7 : model=opus effort=medium agent=android-developer — C1=1 C2=2 C3=0 C4=1 C5=1 (sécurité réseau)")
        self.assertEqual(rc, 0)

    def test_aucune_ligne(self):
        self.assertEqual(check("rien")[0], 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
