import { Store } from "./store";
import type { Item } from "../model/item";
import React from "react";
const lazy = import("./lazy");
// import ghost from "./ghost";
const NOTE = "import fake from './fake'";
export const MAX_ITEMS = 10;
export interface Props { id: string }
export type Mode = "a" | "b";
export class App {
  render(props: Props): string {
    return formatTitle(props.id);
  }
}
export function boot(root) {
  const store = new Store();
  store.load(root);
}
const handler = async (event) => {
  logEvent(event);
};
