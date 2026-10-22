// Run with: npm test (Node's built-in runner; it strips the types itself). All data is made up.
import assert from "node:assert/strict";
import { test } from "node:test";
import { commitmentLines, removeCommitment, replaceCommitment, splitCommitments } from "../lib/commitments.ts";

test("the numbers are split on commas and trimmed", () => {
  assert.deepEqual(splitCommitments("123, 456 ,789"), ["123", "456", "789"]);
  assert.deepEqual(splitCommitments(""), []);
  assert.deepEqual(splitCommitments(" , ,"), []);
});

test("a number with a label stays whole", () => {
  assert.deepEqual(splitCommitments("דרישה 55, דגימה 66"), ["דרישה 55", "דגימה 66"]);
});

test("a form has one line per number, or one empty line if it has none", () => {
  assert.deepEqual(commitmentLines("123, 456"), ["123", "456"]);
  assert.deepEqual(commitmentLines("123"), ["123"]);
  assert.deepEqual(commitmentLines(""), [""]);
});

test("editing a line changes only that number", () => {
  assert.equal(replaceCommitment("123, 456, 789", 1, "999"), "123, 999, 789");
  assert.equal(replaceCommitment("123, 456", 0, "  111  "), "111, 456");
});

test("typing several numbers into one line makes more lines, typing nothing removes the line", () => {
  assert.equal(replaceCommitment("123, 456", 0, "1, 2"), "1, 2, 456");
  assert.equal(replaceCommitment("123, 456", 1, ""), "123");
});

test("a line of a form with no number can be filled in", () => {
  assert.equal(replaceCommitment("", 0, "555"), "555");
});

test("removing a line takes out that number, and an index past the end changes nothing", () => {
  assert.equal(removeCommitment("123, 456, 789", 0), "456, 789");
  assert.equal(removeCommitment("123", 0), "");
  assert.equal(removeCommitment("123, 456", 5), "123, 456");
});
