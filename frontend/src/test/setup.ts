import "@testing-library/jest-dom/vitest";

// Node >= 25 defines a global `localStorage` getter that yields undefined unless
// --localstorage-file is given, and it shadows jsdom's Storage. Always install an
// in-memory Storage (without reading the getter) so the suite does not depend on
// the Node version.
const store = new Map<string, string>();
const memoryStorage = {
  get length() {
    return store.size;
  },
  clear: () => store.clear(),
  getItem: (key: string) => store.get(key) ?? null,
  key: (index: number) => [...store.keys()][index] ?? null,
  removeItem: (key: string) => {
    store.delete(key);
  },
  setItem: (key: string, value: string) => {
    store.set(key, String(value));
  },
} satisfies Storage;
Object.defineProperty(globalThis, "localStorage", { configurable: true, value: memoryStorage });
