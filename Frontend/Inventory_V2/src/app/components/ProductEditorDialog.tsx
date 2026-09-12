import type { ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';

export function ProductEditorDialog({ children, title, busy, onClose, wide = false }: {
  children: ReactNode;
  title: string;
  busy: boolean;
  onClose: () => void;
  wide?: boolean;
}) {
  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[100] bg-slate-950/60 backdrop-blur-sm" />
        <Dialog.Content
          aria-describedby={undefined}
          onEscapeKeyDown={(event) => { if (busy) event.preventDefault(); }}
          onInteractOutside={(event) => { if (busy) event.preventDefault(); }}
          className={`fixed left-1/2 top-1/2 z-[101] flex max-h-[calc(100dvh-1rem)] w-[calc(100%-1rem)] min-w-0 -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-2xl bg-white shadow-2xl outline-none sm:max-h-[calc(100dvh-2rem)] ${wide ? 'max-w-6xl' : 'max-w-2xl'}`}
        >
          <Dialog.Title className="sr-only">{title}</Dialog.Title>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
