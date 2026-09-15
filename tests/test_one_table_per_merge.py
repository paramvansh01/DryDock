"""One GOLDEN table per merge: the gate refuses more, and crash recovery finds the table it was swapping. Offline."""

from __future__ import annotations

from drydock import gate, merge


def test_a_change_to_one_table_passes():
    assert gate.table_blocks([{"table": "GOLDEN.CUSTOMERS"}]) == []
    assert gate.table_blocks([]) == []


def test_a_change_to_two_tables_is_refused():
    assert gate.table_blocks([{"table": "GOLDEN.CUSTOMERS"}, {"table": "GOLDEN.ORDERS"}]) == ["MULTI_TABLE"]


def test_recovery_finds_the_table_from_the_working_copies_left_behind():
    present = {"ORDERS", "ORDERS__NEW_7", "ORDERS__ARCH_7", "CUSTOMERS", "CUSTOMERS__ARCH_3"}
    assert merge.swap_table(present, 7) == "ORDERS"
    assert merge.swap_table({"CUSTOMERS__NEW_9"}, 9) == "CUSTOMERS"


def test_recovery_does_not_guess_when_nothing_or_several_tables_are_left():
    assert merge.swap_table({"CUSTOMERS", "CUSTOMERS__ARCH_3"}, 7) is None
    assert merge.swap_table({"A__NEW_5", "B__ARCH_5"}, 5) is None
