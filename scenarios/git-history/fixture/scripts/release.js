import { readFileSync } from "node:fs";

const commits = readFileSync("release/commits.txt", "utf8").trim().split("\n");
console.log(`${commits.length} commits since the last tag`);
