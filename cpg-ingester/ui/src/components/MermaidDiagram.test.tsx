import { render, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { MermaidDiagram } from './MermaidDiagram';

const mermaid = vi.hoisted(() => ({
  initialize: vi.fn(),
  render: vi.fn().mockResolvedValue({ svg: '<svg data-testid="diagram"><text>Review</text></svg>' }),
}));

vi.mock('mermaid', () => ({ default: mermaid }));

describe('MermaidDiagram', () => {
  it('uses strict security and displays the rendered diagram', async () => {
    const definition = 'flowchart LR; Start-->Review';
    const { container } = render(<MermaidDiagram definition={definition} />);

    await waitFor(() => expect(container.querySelector('svg')).toBeInTheDocument());
    expect(mermaid.initialize).toHaveBeenCalledWith({
      startOnLoad: false, theme: 'default', securityLevel: 'strict',
    });
    expect(mermaid.render).toHaveBeenCalledWith(expect.stringMatching(/^mermaid-/), definition);
    expect(container.querySelector('text')).toHaveTextContent('Review');
  });
});
