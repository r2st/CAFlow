import { useId, useRef, useState } from 'react'
import { ACCEPTED_FILE_TYPES } from './ui'

/**
 * A file picker that uploads as soon as a file is chosen.
 *
 * The native input is visually hidden and driven by its `<label>` — that keeps
 * the control keyboard-reachable and screen-reader-labelled while letting it
 * look like the rest of the buttons.
 */
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
