import { useId, useRef, useState } from 'react'

/**
 * A file picker that uploads as soon as a file is chosen.
 *
 * The native input is visually hidden and driven by its `<label>` — that keeps
 * the control keyboard-reachable and screen-reader-labelled while letting it
 * look like the rest of the buttons.
 */

/**
 * Mirrors ALLOWED_CONTENT_TYPES in the backend's storage service.
 *
 * No .xls or .doc. Those are OLE containers, which the server refuses on their
 * leading bytes whatever content type the browser puts on them, so offering
 * them here only invited a client to pick the one file that could not be sent.
 * The refusal names both formats and says to save as .docx/.xlsx or PDF.
 */
export const ACCEPTED_FILE_TYPES =
  '.pdf,.jpg,.jpeg,.png,.webp,.tif,.tiff,.txt,.csv,.json,.xlsx,.docx'

export default function PortalUpload({
  label = 'Upload',
  busyLabel = 'Uploading…',
  onUpload,
  disabled = false,
  variant = '',
}) {
  const inputId = useId()
  const inputRef = useRef(null)
  const [busy, setBusy] = useState(false)

  async function handleChange(event) {
    const file = event.target.files?.[0]
    if (!file) return
    setBusy(true)
    try {
      await onUpload(file)
    } finally {
      setBusy(false)
      // Clearing the input lets the same file be picked again after a failure.
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  return (
    <span className="upload-control">
      <input
        ref={inputRef}
        id={inputId}
        type="file"
        className="visually-hidden"
        accept={ACCEPTED_FILE_TYPES}
        disabled={disabled || busy}
        onChange={handleChange}
      />
      <label
        htmlFor={inputId}
        className={`upload-button ${variant} ${disabled || busy ? 'is-disabled' : ''}`}
      >
        {busy ? busyLabel : label}
      </label>
    </span>
  )
}
