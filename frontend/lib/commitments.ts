// A form can carry several commitment numbers. The table and the Excel file have one line per
// number: the commitment number is what identifies a line, and every other detail of the form is
// copied onto each of them. The page still holds the numbers as one text ("123, 456", the way the
// backend sends a list), so the lines are derived from that text and every edit goes back into it.

/** The numbers in the text of a commitment cell. */
export function splitCommitments(text: string): string[] {
  return text
    .split(",")
    .map((n) => n.trim())
    .filter(Boolean);
}

/** One entry per line of the table: each number, or a single empty line if the form has none. */
export function commitmentLines(text: string): string[] {
  const numbers = splitCommitments(text);
  return numbers.length > 0 ? numbers : [""];
}

/** Replaces the number on line `index` with what was typed - which may be several numbers
 * ("123, 456" becomes two lines) or nothing (the line goes away). */
export function replaceCommitment(text: string, index: number, typed: string): string {
  const numbers = splitCommitments(text);
  numbers.splice(index, 1, ...splitCommitments(typed));
  return numbers.join(", ");
}

export const removeCommitment = (text: string, index: number) => replaceCommitment(text, index, "");
