// Run with: npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import { FIELD_VALIDATORS, idLengthError, parseExtraction } from "../lib/fields.ts";

test("only exactly nine digits may be saved as an ID number", () => {
  assert.equal(idLengthError("123456782"), null);
  assert.equal(idLengthError("000000018"), null);
  assert.equal(idLengthError(" 123456782 "), null);
  for (const bad of ["11223344", "1", "1234567890", "12345678a", "12-3456782"]) {
    assert.match(idLengthError(bad) ?? "", /9 ספרות/, bad);
  }
});

test("an empty ID number is allowed - an unrecognised field stays empty", () => {
  assert.equal(idLengthError(""), null);
  assert.equal(idLengthError("   "), null);
});

test("the rule is registered for the ID field only", () => {
  assert.equal(FIELD_VALIDATORS.id_number, idLengthError);
  assert.equal(FIELD_VALIDATORS.first_name, undefined);
});

test("a score below the backend's empty threshold is shown as unread, not as a value to check", () => {
  const text = JSON.stringify({
    first_name: null,
    id_number: "123456782",
    confidence: { first_name: 5, id_number: 40 },
    fields_requiring_review: ["first_name", "id_number"],
    thresholds: { empty_below: 10, ok_above: 70 },
  });
  const parsed = parseExtraction(text)!;
  assert.equal(parsed.states.first_name, "unread");
  assert.equal(parsed.states.id_number, "review");
});

test("without thresholds in the response the fallback is 15", () => {
  const parsed = parseExtraction(JSON.stringify({ confidence: { first_name: 14, last_name: 15 }, fields_requiring_review: ["first_name", "last_name"] }))!;
  assert.equal(parsed.states.first_name, "unread");
  assert.equal(parsed.states.last_name, "review");
});
