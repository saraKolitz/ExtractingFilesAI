"use client";

import { useRef, useState } from "react";

interface Props {
  busy: boolean;
  onFiles: (files: File[]) => void;
}

export function UploadZone({ busy, onFiles }: Props) {
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const open = () => inputRef.current?.click();

  return (
    <div
      className={`dropzone${dragging ? " dragging" : ""}${busy ? " busy" : ""}`}
      role="button"
      tabIndex={0}
      aria-label="העלאת קבצים"
      onClick={open}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      }}
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        onFiles(Array.from(e.dataTransfer.files));
      }}
    >
      <div className="dropzone-icon" aria-hidden>
        ⬆
      </div>
      <p className="dropzone-title">{busy ? "מעלה ומפצל לעמודים..." : "גררו לכאן קובצי PDF או תמונות"}</p>
      <p className="dropzone-hint">אפשר לבחור כמה קבצים יחד: סריקות מרובות עמודים וצילומי טלפון. לחצו לבחירה.</p>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept=".pdf,image/*"
        hidden
        onChange={(e) => {
          if (e.target.files) onFiles(Array.from(e.target.files));
          e.target.value = "";
        }}
      />
    </div>
  );
}
