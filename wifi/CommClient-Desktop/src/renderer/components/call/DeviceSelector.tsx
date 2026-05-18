/**
 * DeviceSelector.tsx — In-call device switching dropdown.
 *
 * Allows switching between microphone, camera, and speaker during call.
 */

import React, { useState, useRef, useEffect } from 'react';
import { ChevronDown, Check } from 'lucide-react';

interface DeviceItem {
  deviceId: string;
  label: string;
  kind: string;
  groupId?: string;
}

interface DeviceSelectorProps {
  type: 'audioinput' | 'videoinput' | 'audiooutput';
  devices: DeviceItem[];
  currentDeviceId: string;
  onChange: (deviceId: string) => void;
}

const typeLabels: Record<string, string> = {
  audioinput: 'Microphone',
  videoinput: 'Camera',
  audiooutput: 'Volume2',
};

const DeviceSelector: React.FC<DeviceSelectorProps> = ({
  type,
  devices,
  currentDeviceId,
  onChange,
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const filteredDevices = devices.filter((d) => d.kind === type);
  const currentDevice = filteredDevices.find((d) => d.deviceId === currentDeviceId);

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (
        dropdownRef.current &&
        !dropdownRef.current.contains(e.target as Node)
      ) {
        setIsOpen(false);
      }
    };

    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside);
    }

    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, [isOpen]);

  return (
    <div className="relative" ref={dropdownRef}>
      {/* Dropdown button */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 px-3 py-2 bg-surface-800 hover:bg-surface-700 text-text-200 text-sm font-medium rounded-lg transition-colors"
        title={typeLabels[type]}
      >
        <span className="truncate max-w-xs">
          {currentDevice?.label || `${typeLabels[type]} ${filteredDevices.length > 0 ? '(unavailable)' : '(none)'}`}
        </span>
        <ChevronDown size={16} className={`transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {/* Dropdown menu */}
      {isOpen && filteredDevices.length > 0 && (
        <div className="absolute top-full left-0 mt-2 w-64 bg-surface-800 border border-surface-700 rounded-lg shadow-xl z-50 overflow-hidden">
          <div className="max-h-64 overflow-y-auto">
            {filteredDevices.map((device) => (
              <button
                key={device.deviceId}
                onClick={() => {
                  onChange(device.deviceId);
                  setIsOpen(false);
                }}
                className={`w-full text-left px-4 py-2.5 flex items-center gap-2 transition-colors ${
                  device.deviceId === currentDeviceId
                    ? 'bg-primary-500/20 text-primary-300'
                    : 'text-text-300 hover:bg-surface-700'
                }`}
              >
                {device.deviceId === currentDeviceId && (
                  <Check size={16} className="text-primary-400 flex-shrink-0" />
                )}
                <span className="flex-1 truncate text-sm">{device.label}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* No devices message */}
      {filteredDevices.length === 0 && (
        <div className="absolute top-full left-0 mt-2 w-64 bg-surface-800 border border-surface-700 rounded-lg shadow-xl z-50 p-3">
          <p className="text-xs text-text-400">
            No {typeLabels[type].toLowerCase()} devices found
          </p>
        </div>
      )}
    </div>
  );
};

export default DeviceSelector;
