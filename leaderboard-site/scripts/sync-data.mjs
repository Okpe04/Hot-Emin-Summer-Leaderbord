import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = resolve(root, "..", "data", "hotemin_posts.json");
const destination = resolve(root, "public", "data", "hotemin_posts.json");

mkdirSync(dirname(destination), { recursive: true });
copyFileSync(source, destination);
console.log(`Synced ${source} -> ${destination}`);
