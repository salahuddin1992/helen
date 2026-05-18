import React, { useState, useCallback, useEffect, useContext, createContext } from 'react';
import { Check, XCircle, AlertTriangle, Info, X } from 'lucide-react';

// Simple UUID v4 generator
const generateId = (): string => {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
};

export type ToastType = 'success' | 'error' | 'warning' | 'info';

export interface ToastMessage {
  id: string;
  type: ToastType;
  title: string;
  message?: string;
  duration?: number;
}

interface ToastContextType {
  toast: (options: Omit<ToastMessage, 'id'>) => void;
}

const ToastContext = createContext<ToastContextType | undefined>(undefined);

export const useToast = (): ToastContextType => {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error('useToast must be used within ToastProvider');
  }
  return context;
};

interface ToastItemProps {
  toast: ToastMessage;
  onRemove: (id: string) => void;
}

const ToastItem: React.FC<ToastItemProps> = ({ toast, onRemove }) => {
  useEffect(() => {
    const timer = setTimeout(() => {
      onRemove(toast.id);
    }, toast.duration || 5000);

    return () => clearTimeout(timer);
  }, [toast.id, toast.duration, onRemove]);

  const styles = {
    success: {
      bg: 'bg-green-500',
      icon: <Check size={20} className="text-white" />,
    },
    error: {
      bg: 'bg-red-500',
      icon: <XCircle size={20} className="text-white" />,
    },
    warning: {
      bg: 'bg-amber-500',
      icon: <AlertTriangle size={20} className="text-white" />,
    },
    info: {
      bg: 'bg-blue-500',
      icon: <Info size={20} className="text-white" />,
    },
  };

  const style = styles[toast.type];

  return (
    <div className={`${style.bg} text-white rounded-lg shadow-lg p-4 flex items-start gap-3 min-w-80 animate-slide-in-right`}>
      {style.icon}
      <div className="flex-1 min-w-0">
        <h3 className="font-semibold text-sm">{toast.title}</h3>
        {toast.message && <p className="text-xs mt-1 opacity-90">{toast.message}</p>}
      </div>
      <button
        onClick={() => onRemove(toast.id)}
        className="flex-shrink-0 text-white hover:text-gray-200 transition-colors p-1 -m-1"
        aria-label="Dismiss notification"
      >
        <X size={16} />
      </button>
    </div>
  );
};

interface ToastProviderProps {
  children: React.ReactNode;
}

export const ToastProvider: React.FC<ToastProviderProps> = ({ children }) => {
  const [toasts, setToasts] = useState<ToastMessage[]>([]);

  const toast = useCallback((options: Omit<ToastMessage, 'id'>) => {
    const id = generateId();
    const newToast: ToastMessage = {
      ...options,
      id,
      duration: options.duration || 5000,
    };

    setToasts((prev) => [...prev.slice(-4), newToast]);
  }, []);

  const removeToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-3 pointer-events-none">
        {toasts.map((t) => (
          <div key={t.id} className="pointer-events-auto">
            <ToastItem toast={t} onRemove={removeToast} />
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
};
