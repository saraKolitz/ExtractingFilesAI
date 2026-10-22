"use client";

import { useEffect } from "react";
import type { PageInfo } from "@/lib/api";

export function PageViewer({ page, onClose }: { page: PageInfo; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="overlay" onClick={onClose}>
      <div className="viewer" role="dialog" aria-modal onClick={(e) => e.stopPropagation()}>
        <div className="viewer-bar">
          <span dir="auto">
            {page.file_name} - עמוד {page.page_number}
          </span>
          <button className="link-btn" onClick={onClose}>
            סגירה ✕
          </button>
        </div>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={page.image_url} alt={`${page.file_name} - עמוד ${page.page_number}`} />
      </div>
    </div>
  );
}
