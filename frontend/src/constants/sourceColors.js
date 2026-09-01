/**
 * Per-source-type accent color, shared between GraphView.jsx (node fill,
 * filter panel swatches) and ChatWindow.jsx (data source indicator dots)
 * so the same source always reads as the same color everywhere in the
 * app. This is the one place these colors are defined — do not
 * reintroduce a second, local copy in either component, which is exactly
 * how this file's own values drifted out of sync with GraphView.jsx's for
 * a while (notion/gmail/github had the wrong colors here, and the key was
 * "google_calendar" instead of "calendar", so the calendar dot never
 * matched anything and rendered with no color at all). See DECISIONS.md.
 */
export const SOURCE_TYPE_COLORS = {
  local_file: "#6b4fd6",
  notion: "#c0392b",
  gmail: "#1f9d55",
  // Was "#16151a" then "#e5e4e9" in earlier, drifted copies of this map —
  // both barely visible against this dark theme's background. See
  // DECISIONS.md.
  github: "#3b82f6",
  // Matches extractors/calendar.py's SOURCE_TYPE = "calendar" — was
  // "google_calendar" here, which never matched any real source_type.
  calendar: "#e0a800",
  browser_history: "#8b8896",
};
