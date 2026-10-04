import assert from "node:assert";

import { createWidget, describeWidget } from "../src/widget.js";

const widget = createWidget("gauge", "small");
assert.equal(widget.label, "gauge");
assert.equal(describeWidget(widget), "gauge (small)");
assert.throws(() => createWidget(""));

console.log("ok - widget");
