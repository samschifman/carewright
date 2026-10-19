import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { AutomationParameter } from '../api/types';
import { ParameterDefaultsForm, validateParameterValue } from './ParameterDefaultsForm';

const parameters: AutomationParameter[] = [
  {
    name: 'review_window', type: 'integer', required: true,
    constraints: { min: 1, max: 4 }, description: 'A bounded operational value.',
  },
  { name: 'repeat_interval', type: 'duration', required: true, unit: 'd' },
  { name: 'message_text', type: 'string', required: true, default: 'Authored template text.', source: { kind: 'authored' } },
  { name: 'urgency', type: 'string', required: true, constraints: { enum: ['routine', 'urgent'] } },
];

describe('ParameterDefaultsForm', () => {
  it('renders typed fields, units, enum choices, and authored text', () => {
    render(
      <ParameterDefaultsForm
        templateId="template-1"
        parameters={parameters}
        edits={[]}
        onEditChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('spinbutton', { name: 'Default for review_window' })).toBeInTheDocument();
    expect(screen.getByText('Unit: d')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Default for repeat_interval' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Default for message_text' })).toHaveValue('Authored template text.');
    expect(screen.getByRole('combobox', { name: 'Default for urgency' })).toBeInTheDocument();
  });

  it('emits a typed edit and reports constraint failures', () => {
    const onEditChange = vi.fn();
    render(
      <ParameterDefaultsForm
        templateId="template-1"
        parameters={parameters.slice(0, 1)}
        edits={[]}
        onEditChange={onEditChange}
      />,
    );

    fireEvent.change(screen.getByRole('spinbutton', { name: 'Default for review_window' }), {
      target: { value: '5' },
    });

    expect(onEditChange).toHaveBeenCalledWith(
      'template-1',
      { templateId: 'template-1', parameter: 'review_window', value: 5 },
      'Value must be at most 4.',
      'review_window',
    );
    expect(validateParameterValue(parameters[0], 2)).toBeNull();
    expect(validateParameterValue(parameters[0], 0)).toContain('at least 1');
  });
});
