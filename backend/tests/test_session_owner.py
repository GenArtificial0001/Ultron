"""BL-022 (hallazgos F1/F2, ADR-010): a quien pertenece una sesion y quien la ve en el listado.

`assign_project_path` guarda el usuario de Windows PELADO ("juan_perez"); versiones anteriores guardaban "usuario_yyyymmddhhmmss". El indice maestro
tiene que entender los DOS formatos sin confundir un guion bajo del NOMBRE con el separador del sello de tiempo, y sin que el comodin `_` de LIKE
haga que "jorge" vea las sesiones de "jorge2". Todo con una base temporal (fixture autouse `tmp_master_db`)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import main
from app.persistence import master_index as mi
from app.persistence import service as svc

STAMP = "_20261001231228"


def put(session_id: str, stored_owner: str, path: str = "C:/proyecto") -> None:
    mi.set_project_path(session_id, path, windows_user_created=stored_owner)


# ---------------------------------------------------------------------------------------------------------------------
# get_owner
# ---------------------------------------------------------------------------------------------------------------------
class TestGetOwner:
    def test_a_bare_username_is_returned_as_is(self):
        put("s", "usuario1")
        assert mi.get_owner("s") == "usuario1"

    def test_a_timestamp_suffix_is_stripped(self):
        put("s", "usuario1" + STAMP)
        assert mi.get_owner("s") == "usuario1"

    def test_a_username_with_underscore_plus_timestamp_keeps_its_underscore(self):
        put("s", "juan_perez" + STAMP)
        assert mi.get_owner("s") == "juan_perez"

    @pytest.mark.parametrize("user", ["juan_perez", "ana_maria_lopez", "a_b"])
    def test_a_bare_username_with_underscores_is_not_truncated(self, user):
        put("s", user)
        assert mi.get_owner("s") == user

    def test_no_entry_or_empty_owner_means_no_owner(self):
        assert mi.get_owner("no-existe") is None
        put("s", "")
        assert mi.get_owner("s") is None

    @pytest.mark.parametrize("suffix", ["_2026", "_abc", "_20261001231228x", "_2026100123122", "_202610012312281"])
    def test_only_a_14_digit_suffix_counts_as_a_timestamp(self, suffix):
        """Un sufijo que NO es 14 digitos exactos es parte del nombre: `usuario_2026` no es `usuario` + sello."""
        put("s", "ana" + suffix)
        assert mi.get_owner("s") == "ana" + suffix


# ---------------------------------------------------------------------------------------------------------------------
# list_all_for_user
# ---------------------------------------------------------------------------------------------------------------------
class TestListAllForUser:
    def ids(self, user: str) -> list[str]:
        return sorted(e["session_id"] for e in mi.list_all_for_user(user))

    def test_bare_and_stamped_formats_both_match_their_owner(self):
        put("pelado", "jorge")
        put("sellado", "jorge" + STAMP)

        assert self.ids("jorge") == ["pelado", "sellado"]

    def test_a_session_without_a_registered_owner_is_visible_to_everyone(self):
        """Fail-open documentado: una sesion sin dueño no debe quedar invisible para nadie."""
        put("huerfana", "")

        assert self.ids("cualquiera") == ["huerfana"]

    def test_a_user_does_not_see_sessions_of_a_user_whose_name_starts_the_same(self):
        put("mia", "jorge")
        put("de-jorge2", "jorge2")
        put("de-jorgeX", "jorgeX" + STAMP)

        assert self.ids("jorge") == ["mia"]

    def test_other_users_sessions_are_not_listed(self):
        put("mia", "ana")
        put("ajena", "luis" + STAMP)

        assert self.ids("ana") == ["mia"]

    def test_underscored_usernames_are_listed_for_themselves_and_not_for_their_prefix(self):
        put("de-juan-perez", "juan_perez")
        put("de-juan", "juan")

        assert self.ids("juan_perez") == ["de-juan-perez"]
        assert self.ids("juan") == ["de-juan"]

    def test_newest_first(self):
        put("viejo", "ana")
        put("nuevo", "ana")
        mi.update_title("viejo", "v")  # toca updated_at del primero
        mi.update_title("nuevo", "n")

        ids = [e["session_id"] for e in mi.list_all_for_user("ana")]
        assert ids == ["nuevo", "viejo"]

    def test_a_name_with_sql_wildcards_matches_only_itself(self):
        put("raro", "100%real")
        put("otro", "100Xreal")

        assert self.ids("100%real") == ["raro"]

    def test_no_entries_gives_an_empty_list(self):
        assert mi.list_all_for_user("nadie") == []


# ---------------------------------------------------------------------------------------------------------------------
# Bordes del arreglo
# ---------------------------------------------------------------------------------------------------------------------
class TestOwnerName:
    @pytest.mark.parametrize(
        "stored,expected",
        [
            ("ana", "ana"),
            ("ana" + STAMP, "ana"),
            ("ana_maria" + STAMP, "ana_maria"),
            ("ana_maria", "ana_maria"),
            ("_20261001231228", ""),  # solo sello: nombre vacio (no existe en la practica)
            ("ana_20261001231228_20261001231228", "ana_20261001231228"),  # solo se quita UN sello, el del final
            ("ana_2026100123122x", "ana_2026100123122x"),
            ("", ""),
        ],
    )
    def test_only_one_trailing_14_digit_stamp_is_removed(self, stored, expected):
        assert mi._owner_name(stored) == expected

    def test_the_comparison_stays_case_sensitive_like_before(self):
        put("s", "Ana")
        assert mi.get_owner("s") == "Ana" and mi.get_owner("s") != "ana"

    def test_an_empty_stored_owner_is_still_visible_to_everyone_in_the_listing(self):
        put("a", "")
        put("b", "")
        assert sorted(e["session_id"] for e in mi.list_all_for_user("quien-sea")) == ["a", "b"]

    def test_the_listing_exposes_the_stored_value_untouched(self):
        put("s", "ana" + STAMP)
        [entry] = mi.list_all_for_user("ana")
        assert entry["windows_user_created"] == "ana" + STAMP and entry["session_id"] == "s"

    def test_unicode_names_match_exactly_and_case_sensitively(self):
        put("s", "josé")
        assert [e["session_id"] for e in mi.list_all_for_user("josé")] == ["s"]
        assert mi.list_all_for_user("JOSÉ") == []


# ---------------------------------------------------------------------------------------------------------------------
# El control real que usan los endpoints (BOLA/IDOR): _assert_session_owner
# ---------------------------------------------------------------------------------------------------------------------
class TestAssertSessionOwner:
    @pytest.fixture
    def as_user(self, monkeypatch, tmp_path):
        def _as(name: str):
            monkeypatch.setattr(svc, "current_windows_user", lambda: name)
            monkeypatch.setenv("USERNAME", name)

        folder = tmp_path / "proyecto"
        folder.mkdir()
        return _as, folder

    def test_the_owner_passes(self, as_user):
        as_, folder = as_user
        as_("usuario1")
        svc.assign_project_path("s1", str(folder))

        main._assert_session_owner("s1")  # no lanza

    def test_another_user_gets_403(self, as_user):
        as_, folder = as_user
        as_("usuario1")
        svc.assign_project_path("s1", str(folder))
        as_("otra-persona")

        with pytest.raises(HTTPException) as exc:
            main._assert_session_owner("s1")
        assert exc.value.status_code == 403

    def test_a_session_without_an_owner_passes(self):
        main._assert_session_owner("sesion-solo-en-memoria")

    def test_a_user_with_an_underscore_can_open_his_own_session(self, as_user):
        as_, folder = as_user
        as_("juan_perez")
        svc.assign_project_path("s1", str(folder))

        main._assert_session_owner("s1")  # hoy: HTTPException 403

    def test_a_user_whose_name_is_a_prefix_cannot_open_the_longer_names_session(self, as_user):
        as_, folder = as_user
        as_("juan_perez")
        svc.assign_project_path("s1", str(folder))
        as_("juan")

        with pytest.raises(HTTPException) as exc:
            main._assert_session_owner("s1")
        assert exc.value.status_code == 403
