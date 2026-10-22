// Run with: npm test. All data is made up.
import assert from "node:assert/strict";
import { test } from "node:test";
import { calibrate, fieldOutcome, type CalibrationRow } from "../lib/calibration.ts";

const FIELDS = ["first_name", "last_name"] as const;
const blank = { first_name: "", last_name: "", id_number: "", central_numbers: "", received_date: "", sending_entity: "", commitment_numbers: "" };

// A done row whose first_name was read as "דנה" with the given score, then reviewed as asked.
function row(score: number, review: "confirm" | "fix" | "skip", okAbove = 70): CalibrationRow {
  const original = { ...blank, first_name: "דנה" };
  return {
    status: "done",
    original,
    values: { ...original, first_name: review === "fix" ? "דנית" : "דנה" },
    confidence: { first_name: score },
    edited: review === "skip" ? {} : { first_name: true },
    okAbove,
  };
}

test("an untouched field is unreviewed, a confirmed one is right, a changed one is wrong", () => {
  assert.equal(fieldOutcome(row(80, "skip"), "first_name"), "unreviewed");
  assert.equal(fieldOutcome(row(80, "confirm"), "first_name"), "confirmed");
  assert.equal(fieldOutcome(row(80, "fix"), "first_name"), "typed");
});

test("retyping the model's own value counts as confirming it", () => {
  const r = row(80, "confirm");
  r.values.first_name = " דנה ";
  assert.equal(fieldOutcome(r, "first_name"), "confirmed");
});

test("nothing reviewed gives no accuracy and asks for data", () => {
  const c = calibrate([row(80, "skip"), row(90, "skip")], FIELDS);
  assert.equal(c.reviewed, 0);
  assert.equal(c.accuracy, null);
  assert.deepEqual(c.suggestion, { kind: "need-data", have: 0, need: 10 });
});

test("rows that are not done and fields without a score are ignored", () => {
  const pending = { ...row(80, "confirm"), status: "pending" };
  const unscored = { ...row(80, "confirm"), confidence: {} };
  assert.equal(calibrate([pending, unscored], FIELDS).reviewed, 0);
});

test("bands count reviewed fields and their accuracy by score", () => {
  const rows = [row(95, "confirm"), row(92, "confirm"), row(75, "confirm"), row(72, "fix"), row(5, "fix")];
  const c = calibrate(rows, FIELDS);
  assert.equal(c.reviewed, 5);
  assert.equal(c.correct, 3);
  const band = (from: number) => c.bands.find((b) => b.from === from)!;
  assert.deepEqual([band(90).reviewed, band(90).correct, band(90).accuracy], [2, 2, 1]);
  assert.deepEqual([band(70).reviewed, band(70).correct, band(70).accuracy], [2, 1, 0.5]);
  assert.deepEqual([band(0).reviewed, band(0).correct], [1, 0]);
  assert.equal(band(50).accuracy, null);
});

test("a field the model left empty and the human filled in counts as a miss in the lowest band", () => {
  const r = row(0, "fix");
  r.original.first_name = "";
  r.values.first_name = "דנה";
  const c = calibrate([r], FIELDS);
  assert.equal(c.bands[0].reviewed, 1);
  assert.equal(c.bands[0].correct, 0);
});

test("too few reviewed fields means no recommendation", () => {
  const rows = Array.from({ length: 9 }, () => row(95, "confirm"));
  assert.deepEqual(calibrate(rows, FIELDS).suggestion, { kind: "need-data", have: 9, need: 10 });
});

test("lowers the clean line when lower scores were just as reliable, and counts the reviews saved", () => {
  // Flagged today (<= 70) yet always right from 50 up; plus one unreviewed flagged field at 60.
  const rows = [
    ...Array.from({ length: 6 }, () => row(55, "confirm")),
    ...Array.from({ length: 6 }, () => row(85, "confirm")),
    row(20, "fix"),
    row(60, "skip"),
  ];
  const s = calibrate(rows, FIELDS).suggestion;
  assert.equal(s.kind, "lower");
  if (s.kind === "lower") {
    assert.equal(s.cutoff, 55);
    assert.equal(s.current, 71);
    assert.equal(s.accuracy, 1);
    assert.equal(s.saved, 7); // the six at 55 and the unreviewed one at 60
  }
});

test("raises the clean line when scores just above it were often wrong", () => {
  const rows = [
    ...Array.from({ length: 4 }, () => row(75, "fix")),
    ...Array.from({ length: 2 }, () => row(75, "confirm")),
    ...Array.from({ length: 8 }, () => row(96, "confirm")),
  ];
  const s = calibrate(rows, FIELDS).suggestion;
  assert.equal(s.kind, "raise");
  if (s.kind === "raise") assert.equal(s.cutoff, 96);
});

test("keeps the line when it already sits where the data says", () => {
  const rows = [...Array.from({ length: 6 }, () => row(40, "fix")), ...Array.from({ length: 8 }, () => row(71, "confirm"))];
  const s = calibrate(rows, FIELDS).suggestion;
  assert.equal(s.kind, "keep");
});

test("says so when no score is accurate enough", () => {
  const rows = Array.from({ length: 12 }, (_, i) => row(90 + (i % 5), i % 2 ? "fix" : "confirm"));
  assert.deepEqual(calibrate(rows, FIELDS).suggestion, { kind: "none" });
});
