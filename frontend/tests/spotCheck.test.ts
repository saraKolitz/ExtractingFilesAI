// Run with: npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import { pickSpotChecks, type Cell } from "../lib/spotCheck.ts";

const cells = (n: number): Cell[] => Array.from({ length: n }, (_, i) => ({ rowKey: `row-${i}`, field: "first_name" }));

test("about a tenth of the clean fields are picked", () => {
  assert.equal(pickSpotChecks(cells(100)).size, 10);
  assert.equal(pickSpotChecks(cells(101)).size, 11); // rounded up
});

test("a small batch still gets a minimum sample, never more than it has", () => {
  assert.equal(pickSpotChecks(cells(20)).size, 3);
  assert.equal(pickSpotChecks(cells(2)).size, 2);
  assert.equal(pickSpotChecks(cells(0)).size, 0);
});

test("the sample is the same every time and does not depend on the order of the rows", () => {
  const a = pickSpotChecks(cells(60));
  const b = pickSpotChecks([...cells(60)].reverse());
  assert.deepEqual([...a].sort(), [...b].sort());
});

test("the picked cells are real cells of the input", () => {
  const input = cells(40);
  const ids = new Set(input.map((c) => `${c.rowKey}|${c.field}`));
  for (const id of pickSpotChecks(input)) assert.ok(ids.has(id));
});

test("the sample is spread over the batch, not just the first rows", () => {
  const picked = [...pickSpotChecks(cells(200))].map((id) => Number(id.split("|")[0].slice(4)));
  assert.ok(Math.max(...picked) > 100 && Math.min(...picked) < 100);
});
