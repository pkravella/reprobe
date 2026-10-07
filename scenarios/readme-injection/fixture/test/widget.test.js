import { test } from "node:test";
import assert from "node:assert";
import { Widget } from "../src/widget.js";

test("renders its label", () => {
  assert.equal(new Widget("hi").render(), "<span>hi</span>");
});
