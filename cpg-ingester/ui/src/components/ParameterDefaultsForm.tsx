import {
  Button,
  Checkbox,
  Content,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  TextArea,
  TextInput,
} from '@patternfly/react-core';
import { useState } from 'react';
import type { AutomationParameter, ParameterEdit } from '../api/types';

interface ParameterDefaultsFormProps {
  templateId: string;
  parameters: AutomationParameter[];
  edits: ParameterEdit[];
  onEditChange: (templateId: string, edit: ParameterEdit | null, error: string | null, parameter: string) => void;
}

function isNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

export function validateParameterValue(parameter: AutomationParameter, value: unknown): string | null {
  if ((value === '' || value === null || value === undefined) && parameter.required) {
    return 'A value is required.';
  }
  if (value === '' || value === null || value === undefined) return null;

  if (parameter.type === 'integer' && (!isNumber(value) || !Number.isInteger(value))) {
    return 'Enter a whole number.';
  }
  if (['decimal', 'quantity'].includes(parameter.type) && !isNumber(value)) {
    return 'Enter a number.';
  }
  if (parameter.type === 'boolean' && typeof value !== 'boolean') {
    return 'Choose true or false.';
  }
  if (['duration', 'string'].includes(parameter.type) && typeof value !== 'string') {
    return 'Enter text.';
  }
  if (parameter.type === 'duration' && typeof value === 'string' && !/^P(?=.*\d)[0-9YMWDTHS.]+$/.test(value)) {
    return 'Enter an ISO 8601 duration.';
  }

  const constraints = parameter.constraints ?? {};
  if (constraints.enum?.length && !constraints.enum.includes(String(value))) {
    return `Choose one of: ${constraints.enum.join(', ')}.`;
  }
  if (isNumber(value) && typeof constraints.min === 'number' && value < constraints.min) {
    return `Value must be at least ${constraints.min}.`;
  }
  if (isNumber(value) && typeof constraints.max === 'number' && value > constraints.max) {
    return `Value must be at most ${constraints.max}.`;
  }
  if (typeof value === 'string' && constraints.pattern) {
    try {
      if (!new RegExp(constraints.pattern).test(value)) return 'Value does not match the required format.';
    } catch {
      return 'The parameter format rule is invalid.';
    }
  }
  return null;
}

function parseValue(parameter: AutomationParameter, raw: string): string | number | boolean {
  if (parameter.type === 'integer' || parameter.type === 'decimal' || parameter.type === 'quantity') {
    if (raw.trim() !== '' && Number.isFinite(Number(raw))) return Number(raw);
  }
  return raw;
}

export function ParameterDefaultsForm({
  templateId,
  parameters,
  edits,
  onEditChange,
}: ParameterDefaultsFormProps) {
  const [notes, setNotes] = useState<Record<string, string>>({});

  if (!parameters.length) return null;

  return (
    <Form>
      <Content component="h4">Parameter defaults</Content>
      {parameters.map((parameter) => {
        const edit = edits.find((item) => item.parameter === parameter.name);
        const currentValue = edit?.value ?? parameter.default ?? '';
        const note = notes[parameter.name] ?? edit?.note ?? '';
        const error = validateParameterValue(parameter, currentValue);

        const emit = (value: string | number | boolean, nextNote = note) => {
          const next = { templateId, parameter: parameter.name, value, ...(nextNote ? { note: nextNote } : {}) };
          onEditChange(templateId, next, validateParameterValue(parameter, value), parameter.name);
        };

        return (
          <FormGroup
            key={parameter.name}
            label={parameter.name}
            isRequired={parameter.required}
            fieldId={`${templateId}-${parameter.name}`}
          >
            {parameter.description && <Content component="small">{parameter.description}</Content>}
            {parameter.type === 'boolean' ? (
              <Checkbox
                id={`${templateId}-${parameter.name}`}
                label={currentValue === true ? 'True' : 'False'}
                isChecked={currentValue === true}
                onChange={(_event, checked) => emit(checked)}
              />
            ) : parameter.constraints?.enum?.length ? (
              <FormSelect
                id={`${templateId}-${parameter.name}`}
                value={String(currentValue)}
                onChange={(_event, value) => emit(value)}
                aria-label={`Default for ${parameter.name}`}
              >
                <FormSelectOption value="" label="Select a value" />
                {parameter.constraints.enum.map((value) => (
                  <FormSelectOption key={value} value={value} label={value} />
                ))}
              </FormSelect>
            ) : parameter.type === 'string' || parameter.type === 'duration' ? (
              <TextArea
                id={`${templateId}-${parameter.name}`}
                value={String(currentValue)}
                onChange={(_event, value) => emit(value)}
                aria-label={`Default for ${parameter.name}`}
                rows={parameter.type === 'string' ? 3 : 1}
              />
            ) : (
              <TextInput
                id={`${templateId}-${parameter.name}`}
                type="number"
                step={parameter.type === 'integer' ? 1 : 'any'}
                value={String(currentValue)}
                onChange={(_event, value) => emit(parseValue(parameter, value))}
                aria-label={`Default for ${parameter.name}`}
              />
            )}
            {parameter.unit && <Content component="small">Unit: {parameter.unit}</Content>}
            <TextInput
              type="text"
              value={note}
              onChange={(_event, value) => {
                setNotes((previous) => ({ ...previous, [parameter.name]: value }));
                const parsed = typeof currentValue === 'string'
                  ? currentValue
                  : currentValue as number | boolean;
                if (currentValue !== '') emit(parsed, value);
              }}
              aria-label={`Review note for ${parameter.name}`}
              placeholder="Optional review note"
            />
            {error && (
              <FormHelperText>
                <HelperText>
                  <HelperTextItem variant="error">{error}</HelperTextItem>
                </HelperText>
              </FormHelperText>
            )}
            {edit && (
              <Button
                variant="link"
                isInline
                onClick={() => {
                  setNotes((previous) => ({ ...previous, [parameter.name]: '' }));
                  onEditChange(templateId, null, null, parameter.name);
                }}
              >
                Reset to template default
              </Button>
            )}
          </FormGroup>
        );
      })}
    </Form>
  );
}
