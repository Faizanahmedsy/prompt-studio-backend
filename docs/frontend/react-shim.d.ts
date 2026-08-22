/**
 * TYPECHECK-ONLY SHIM — DO NOT COPY THIS FILE INTO THE NEXT APP.
 *
 * This folder is documentation living inside the *backend* repo, where
 * `node_modules` holds no `react`, no `zustand` and no `@types/node`. These
 * ambient declarations exist purely so `tsc --noEmit` can verify the files
 * beside it in isolation. The Next app has the real packages, whose types are
 * strictly richer; copying this file across would shadow them.
 *
 * Only the members these files actually use are declared.
 */

declare module "react" {
  export type DependencyList = readonly unknown[]
  export type Dispatch<A> = (value: A) => void
  export type SetStateAction<S> = S | ((prev: S) => S)
  export interface MutableRefObject<T> {
    current: T
  }

  export function useState<S>(initialState: S | (() => S)): [S, Dispatch<SetStateAction<S>>]
  export function useEffect(effect: () => void | (() => void), deps?: DependencyList): void
  export function useRef<T>(initialValue: T): MutableRefObject<T>
  export function useCallback<T extends (...args: never[]) => unknown>(
    callback: T,
    deps: DependencyList
  ): T
  export function useMemo<T>(factory: () => T, deps: DependencyList): T
}

declare module "zustand" {
  export type StoreApi<T> = {
    getState: () => T
    setState: (partial: T | Partial<T> | ((state: T) => T | Partial<T>), replace?: false) => void
    subscribe: (listener: (state: T, previous: T) => void) => () => void
  }

  export type StateCreator<T> = (
    set: StoreApi<T>["setState"],
    get: StoreApi<T>["getState"],
    api: StoreApi<T>
  ) => T

  export type UseBoundStore<T> = {
    (): T
    <U>(selector: (state: T) => U): U
  } & StoreApi<T>

  /** zustand v5's curried form: `create<T>()((set, get) => ({ … }))`. */
  export function create<T>(): (initializer: StateCreator<T>) => UseBoundStore<T>
  export function create<T>(initializer: StateCreator<T>): UseBoundStore<T>
}

/** Next inlines `process.env.NEXT_PUBLIC_*` at build time; `@types/node`
 *  supplies this declaration in the real app. */
declare const process: {
  env: Record<string, string | undefined>
}
