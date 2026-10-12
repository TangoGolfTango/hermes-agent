/**
 * What the composer's keys do while a turn runs. Plain Enter steers a
 * text-only draft (Cursor-style stop-and-correct) and Cmd/Ctrl+Enter queues it
 * for the next turn, so both live-turn actions stay reachable by keyboard.
 * `display.busy_input_mode: queue` (CLI parity) swaps the two: plain Enter
 * queues and Cmd/Ctrl+Enter steers.
 */

import type { RefObject } from 'react'

import { useHermesConfigRecord } from '@/app/hooks/use-config-record'

import { liveComposerDraft } from './composer-utils'

export type BusyAction = 'queue' | 'steer' | 'stop'

/** Whether the profile's config asks plain Enter to queue while busy. */
export function busyEnterQueues(config: Record<string, unknown> | undefined): boolean {
  const display = config?.display as Record<string, unknown> | undefined

  return (
    String(display?.busy_input_mode ?? '')
      .trim()
      .toLowerCase() === 'queue'
  )
}

export function useBusyEnterQueues(profile: null | string | undefined): boolean {
  const { data } = useHermesConfigRecord(profile ?? undefined)

  return busyEnterQueues(data)
}

interface BusyActionInput {
  canSteer: boolean
  compacting: boolean
  enterQueues: boolean
  hasComposerPayload: boolean
}

/** The busy send button and plain Enter: text redirects the live turn,
 *  attachments queue for the next turn, an empty composer stops. Queue mode
 *  never steers here. */
export function resolveBusyAction({
  canSteer,
  compacting,
  enterQueues,
  hasComposerPayload
}: BusyActionInput): BusyAction {
  if (canSteer && !enterQueues) {
    return 'steer'
  }

  return compacting || hasComposerPayload ? 'queue' : 'stop'
}

/** Cmd/Ctrl+Enter while busy takes the live-turn action plain Enter does not. */
export function busyModEnterAction(enterQueues: boolean, canSteer: boolean): 'queue' | 'steer' {
  return enterQueues && canSteer ? 'steer' : 'queue'
}

/** Source the just-typed content from the DOM so a fast keypress cannot act on
 *  a stale draft: the composer state lags the editor by a render. */
export function syncLiveDraft(
  editor: HTMLElement | null,
  draftRef: RefObject<string>,
  setComposerText: (text: string) => void
): void {
  const editorText = liveComposerDraft(editor, draftRef.current)

  if (editorText !== draftRef.current) {
    draftRef.current = editorText
    setComposerText(editorText)
  }
}
