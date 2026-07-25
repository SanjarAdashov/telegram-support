import pytest

from support_bot import (
    TicketStore,
    find_faq_answer,
    message_targets_bot,
    normalize,
    parse_operator_ids,
    remove_bot_mention,
)


def test_normalize_and_exact_faq_match():
    assert normalize("  Какие часы работы?! ") == "какие часы работы"
    assert find_faq_answer("Какие часы работы?")
    assert find_faq_answer("Сколько стоит доставка?") is None


def test_ticket_lifecycle_and_one_active_ticket(tmp_path):
    store = TicketStore(str(tmp_path / "support.sqlite3"))
    first = store.create_or_get(10, "client", "Сложный вопрос")
    same = store.create_or_get(10, "client", "Дополнение")

    assert first.id == same.id
    assert store.active(10).id == first.id
    messages = store.connection.execute(
        "SELECT body FROM messages WHERE ticket_id=? ORDER BY id", (first.id,)
    ).fetchall()
    assert [row[0] for row in messages] == ["Сложный вопрос", "Дополнение"]

    assert store.close(first.id) is True
    assert store.active(10) is None
    assert store.close(first.id) is False


def test_operator_ids_are_numeric():
    assert parse_operator_ids("123, 456") == {123, 456}
    with pytest.raises(RuntimeError):
        parse_operator_ids("abc")


def test_group_messages_require_bot_mention():
    class Obj:
        pass

    update = Obj()
    update.effective_chat = Obj()
    update.effective_chat.type = "supergroup"
    update.message = Obj()
    update.message.text = "@support_bot Когда вы работаете?"

    assert message_targets_bot(update, "support_bot") is True
    assert message_targets_bot(update, "other_bot") is False
    assert remove_bot_mention(update.message.text, "support_bot") == "Когда вы работаете?"


def test_private_messages_do_not_require_mention():
    class Obj:
        pass

    update = Obj()
    update.effective_chat = Obj()
    update.effective_chat.type = "private"
    update.message = Obj()
    update.message.text = "Вопрос"

    assert message_targets_bot(update, "support_bot") is True
