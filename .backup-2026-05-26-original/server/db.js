import Database from "better-sqlite3";
import * as sqliteVec from "sqlite-vec";
import path from "node:path";
import fs from "node:fs/promises";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);
const DB_PATH     = path.join(__dirname, "..", "data", "maahi.db");
const SCHEMA_PATH = path.join(__dirname, "memory", "schema.sql");

let _db = null;

export function getDb() {
  if (_db) return _db;
  _db = new Database(DB_PATH);
  _db.pragma("journal_mode = WAL");
  _db.pragma("synchronous = NORMAL");
  _db.pragma("foreign_keys = ON");
  sqliteVec.load(_db);
  return _db;
}

export async function initDb() {
  const db = getDb();
  const schema = await fs.readFile(SCHEMA_PATH, "utf8");
  db.exec(schema);
  return db;
}

export function closeDb() {
  if (_db) { _db.close(); _db = null; }
}
