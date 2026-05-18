import React, { useEffect, useState } from 'react';
import { fetchAuthorizedBlobUrl } from '@/services/api.client';

interface AuthorizedImageProps extends React.ImgHTMLAttributes<HTMLImageElement> {
  // Relative API path, e.g. /api/users/{id}/photos/{pid}/image
  path: string;
  fallback?: React.ReactNode;
}

/**
 * Renders an <img> whose source is fetched with the current access token and
 * then turned into a blob URL. Handles refetch when `path` changes and revokes
 * the blob URL on unmount.
 */
export const AuthorizedImage: React.FC<AuthorizedImageProps> = ({
  path,
  fallback,
  alt,
  ...imgProps
}) => {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let currentUrl: string | null = null;
    setError(false);
    setObjectUrl(null);

    fetchAuthorizedBlobUrl(path)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        currentUrl = url;
        setObjectUrl(url);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });

    return () => {
      cancelled = true;
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [path]);

  if (error) return <>{fallback ?? null}</>;
  if (!objectUrl) return <>{fallback ?? null}</>;
  return <img src={objectUrl} alt={alt ?? ''} {...imgProps} />;
};

export default AuthorizedImage;
