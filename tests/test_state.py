import pytest


def test_notes_add_replace_delete_keep_history(state):
    r = state.update_note("question_types", "add", "No true/false questions", "She removed them twice")
    nid = r["note_id"]
    assert r["status"] == "added"
    assert "No true/false" in state.notes_markdown()
    # same note again: no duplicate
    assert state.update_note("question_types", "add", "no true/false questions", "again")["status"] == "already_saved"
    state.update_note("question_types", "replace", "No true/false or yes/no questions", "widened", nid)
    assert "yes/no" in state.notes_markdown()
    state.update_note("question_types", "delete", "", "she wants them back", nid)
    assert "yes/no" not in state.notes_markdown()
    # nothing is hard-deleted
    assert len(state.notes(include_inactive=True)) == 1
    actions = [h["action"] for h in state.history(nid)]
    assert actions == ["add", "replace", "delete"]


def test_restore_is_admin_only_and_works(state):
    nid = state.update_note("format_layout", "add", "Answer key on a separate page", "asked")["note_id"]
    state.update_note("format_layout", "replace", "Answer key at the end", "changed", nid)
    first = state.history(nid)[0]["id"]
    state.restore(first)
    assert "separate page" in state.notes_markdown()
    assert state.history(nid)[-1]["action"] == "restore"


def test_forgiving_section_and_action_names(state):
    r = state.update_note("format", "add", "Use 12pt font", "pref")
    assert r["section"] == "format_layout"
    state.update_note("format", "edit", "Use 14pt font", "bigger", r["note_id"])
    assert "14pt" in state.notes_markdown()


def test_bad_inputs_are_helpful(state):
    with pytest.raises(ValueError, match="paper_preferences"):
        state.update_note("misc", "add", "x", "y")
    with pytest.raises(ValueError, match="note_id"):
        state.update_note("corrections", "replace", "x", "y")
    with pytest.raises(ValueError, match="get_teacher_notes"):
        state.update_note("corrections", "delete", "", "y", 999)


def test_paper_log_and_past_questions(state):
    qs = [{"section": "A", "type": "mcq", "marks": 1, "text": "Which is a magnetic material?"},
          {"section": "B", "type": "short", "marks": 2, "text": "Name the two poles of a magnet."}]
    p1 = state.log_paper(6, "sci", ["6-sci-04"], 20, qs)
    state.log_paper(6, "sci", ["6-sci-05"], 10, qs[:1])
    p3 = state.log_paper(6, "sci", ["6-sci-04", "6-sci-05"], 30, qs[1:])
    past = state.past_questions("6-sci-04", 5)
    assert [p["paper_id"] for p in past] == [p3, p1]
    assert past[1]["questions"][0]["text"] == "Which is a magnetic material?"
    assert len(state.past_questions("6-sci-04", 1)) == 1


def test_state_survives_reopen(tmp_path):
    from server.state import State
    url = f"sqlite:///{tmp_path / 's.db'}"
    State(url).update_note("corrections", "add", "Use 'Curiosity' chapter names", "x")
    assert "Curiosity" in State(url).notes_markdown()
