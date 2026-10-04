/** A widget with a label and an optional size. */
export function createWidget(label, size = "medium") {
  if (!label) {
    throw new Error("createWidget: label is required");
  }
  return { label, size, createdAt: Date.now() };
}

export function describeWidget(widget) {
  return `${widget.label} (${widget.size})`;
}
