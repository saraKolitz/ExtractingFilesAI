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
  const res = await fetch(`${API_URL}/upload`, { method: "POST", body: form });
  if (!res.ok) throw new Error(`שגיאת שרת (${res.status})`);
  return (await res.json()) as UploadResponse;
}
