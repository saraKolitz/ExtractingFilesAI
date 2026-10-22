export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface PageInfo {
  file_id: string;
  file_name: string;
  page_number: number;
  image_url: string;
  thumbnail_url: string;
  width: number;
  height: number;
}

export interface UploadError {
  file_name: string;
  detail: string;
}

export interface UploadResponse {
  pages: PageInfo[];
  errors: UploadError[];
}

export async function uploadFiles(files: File[]): Promise<UploadResponse> {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  let res: Response;
  try {
    res = await fetch(`${API_URL}/upload`, { method: "POST", body: form });
  } catch {
    throw new Error("לא ניתן להתחבר לשרת");
  }
  if (!res.ok) throw new Error(`שגיאת שרת (${res.status})`);
  return (await res.json()) as UploadResponse;
}

export async function extractPage(fileId: string, pageNumber: number): Promise<string> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/extract/${fileId}/${pageNumber}`, { method: "POST" });
  } catch {
    throw new Error("לא ניתן להתחבר לשרת");
  }
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
    const detail = typeof body?.detail === "string" ? body.detail : null;
    throw new Error(detail ? `החילוץ נכשל: ${detail}` : `שגיאת שרת (${res.status})`);
  }
  return ((await res.json()) as { text: string }).text;
}

export interface ExportRow {
  values: Record<string, string>;
  link: string;
}

/** Sends the (possibly hand-corrected) rows to the server and returns the generated .xlsx. */
export async function exportExcel(rows: ExportRow[]): Promise<Blob> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows }),
    });
  } catch {
    throw new Error("לא ניתן להתחבר לשרת");
  }
  if (!res.ok) throw new Error(`הייצוא נכשל (${res.status})`);
  return res.blob();
}
