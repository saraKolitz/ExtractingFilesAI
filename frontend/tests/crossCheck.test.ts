// Run with: npm test (Node's built-in runner; it strips the types itself). All data is made up.
import assert from "node:assert/strict";
import { test } from "node:test";
import { findConflicts } from "../lib/crossCheck.ts";

const row = (key: string, v: Partial<Record<string, string>>) => ({
  key,
  values: {
    first_name: "", last_name: "", id_number: "", central_numbers: "",
    received_date: "", sending_entity: "", commitment_numbers: "", ...v,
  },
});

const ID_A = "123456782";
const ID_B = "000000018";

test("an unrelated batch has no findings", () => {
  const out = findConflicts([
    row("a", { first_name: "דנה", last_name: "לוי", id_number: ID_A, central_numbers: "111" }),
    row("b", { first_name: "יוסי", last_name: "כהן", id_number: ID_B, central_numbers: "222" }),
  ]);
  assert.deepEqual(out, {});
});

test("the same central number twice is a duplicate on both rows", () => {
  const out = findConflicts([row("a", { central_numbers: "111, 222" }), row("b", { central_numbers: "222" })]);
  assert.equal(out.a.central_numbers?.kind, "duplicate");
  assert.match(out.a.central_numbers!.text, /שורה 2/);
  assert.match(out.b.central_numbers!.text, /שורה 1/);
});

test("the same central number with two different valid IDs is a contradiction", () => {
  const out = findConflicts([
    row("a", { id_number: ID_A, central_numbers: "111" }),
    row("b", { id_number: ID_B, central_numbers: "111" }),
  ]);
  assert.equal(out.a.central_numbers?.kind, "conflict");
  assert.equal(out.b.central_numbers?.kind, "conflict");
});

test("an invalid ID is not used to call two rows contradictory", () => {
  const out = findConflicts([
    row("a", { id_number: "11223344", central_numbers: "111" }),
    row("b", { id_number: ID_B, central_numbers: "111" }),
  ]);
  assert.equal(out.a.central_numbers?.kind, "duplicate");
});

test("a commitment number repeated for the same sender is a duplicate", () => {
  const out = findConflicts([
    row("a", { sending_entity: "כללית", commitment_numbers: "555" }),
    row("b", { sending_entity: "כללית", commitment_numbers: "555, 556" }),
  ]);
  assert.equal(out.a.commitment_numbers?.kind, "duplicate");
  assert.equal(out.b.commitment_numbers?.kind, "duplicate");
});

test("the same commitment number from different senders, or with no sender, is not a duplicate", () => {
  assert.deepEqual(
    findConflicts([
      row("a", { sending_entity: "כללית", commitment_numbers: "555" }),
      row("b", { sending_entity: "מכבי", commitment_numbers: "555" }),
    ]),
    {},
  );
  assert.deepEqual(findConflicts([row("a", { commitment_numbers: "555" }), row("b", { commitment_numbers: "555" })]), {});
});

test("the same valid ID with a different name flags the differing name fields only", () => {
  const out = findConflicts([
    row("a", { first_name: "דנה", last_name: "לוי", id_number: ID_A }),
    row("b", { first_name: "דנה", last_name: "כהן", id_number: ID_A }),
  ]);
  assert.equal(out.a.first_name, undefined);
  assert.equal(out.a.last_name?.kind, "conflict");
  assert.equal(out.b.last_name?.kind, "conflict");
});

test("the same ID with an empty name is not a contradiction", () => {
  const out = findConflicts([
    row("a", { first_name: "דנה", last_name: "לוי", id_number: ID_A }),
    row("b", { id_number: ID_A }),
  ]);
  assert.deepEqual(out, {});
});

test("the same full name with different valid IDs flags the ID cells", () => {
  const out = findConflicts([
    row("a", { first_name: "דנה", last_name: "לוי", id_number: ID_A }),
    row("b", { first_name: " דנה", last_name: "לוי ", id_number: ID_B }),
  ]);
  assert.equal(out.a.id_number?.kind, "conflict");
  assert.equal(out.b.id_number?.kind, "conflict");
});

test("a repeated invalid ID (the 11223344 dummy) never triggers name contradictions", () => {
  const out = findConflicts([
    row("a", { first_name: "דנה", last_name: "לוי", id_number: "11223344" }),
    row("b", { first_name: "יוסי", last_name: "כהן", id_number: "11223344" }),
  ]);
  assert.deepEqual(out, {});
});
