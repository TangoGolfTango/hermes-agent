import { describe, expect, it, vi } from 'vitest'

import { busyEnterQueues, busyModEnterAction, resolveBusyAction, syncLiveDraft } from './busy-input-mode'
import { liveComposerDraft } from './composer-utils'

describe('busyEnterQueues', () => {
  it('reads display.busy_input_mode: queue, tolerating case and whitespace', () => {
    expect(busyEnterQueues({ display: { busy_input_mode: ' Queue ' } })).toBe(true)
  })

  it('keeps Enter steering for other modes and a missing config', () => {
    expect(busyEnterQueues({ display: { busy_input_mode: 'interrupt' } })).toBe(false)
    expect(busyEnterQueues({ display: {} })).toBe(false)
    expect(busyEnterQueues({})).toBe(false)
    expect(busyEnterQueues(undefined)).toBe(false)
  })
})

describe('resolveBusyAction', () => {
  const base = { canSteer: false, compacting: false, enterQueues: false, hasComposerPayload: false }

  it('steers a steerable draft by default', () => {
    expect(resolveBusyAction({ ...base, canSteer: true, hasComposerPayload: true })).toBe('steer')
  })

  it('queues a steerable draft in queue mode', () => {
    expect(resolveBusyAction({ ...base, canSteer: true, enterQueues: true, hasComposerPayload: true })).toBe('queue')
  })

  it('queues an unsteerable payload or any input during compaction', () => {
    expect(resolveBusyAction({ ...base, hasComposerPayload: true })).toBe('queue')
    expect(resolveBusyAction({ ...base, compacting: true })).toBe('queue')
  })

  it('stops on an empty composer, in either mode', () => {
    expect(resolveBusyAction(base)).toBe('stop')
    expect(resolveBusyAction({ ...base, enterQueues: true })).toBe('stop')
  })
})

describe('busyModEnterAction', () => {
  it('is the action plain Enter does not take', () => {
    expect(busyModEnterAction(false, true)).toBe('queue')
    expect(busyModEnterAction(true, true)).toBe('steer')
  })

  it('queues when the draft cannot steer, in either mode', () => {
    expect(busyModEnterAction(false, false)).toBe('queue')
    expect(busyModEnterAction(true, false)).toBe('queue')
  })
})

describe('syncLiveDraft', () => {
  it('adopts editor text the composer state has not seen yet', () => {
    const editor = document.createElement('div')
    editor.textContent = 'typed fast'
    const draftRef = { current: '' }
    const setComposerText = vi.fn()

    syncLiveDraft(editor, draftRef, setComposerText)

    expect(draftRef.current).toBe(liveComposerDraft(editor, ''))
    expect(draftRef.current).toContain('typed fast')
    expect(setComposerText).toHaveBeenCalledWith(draftRef.current)
  })

  it('leaves an in-sync draft alone', () => {
    const editor = document.createElement('div')
    editor.textContent = 'same'
    const draftRef = { current: liveComposerDraft(editor, '') }
    const setComposerText = vi.fn()

    syncLiveDraft(editor, draftRef, setComposerText)

    expect(setComposerText).not.toHaveBeenCalled()
  })
})
