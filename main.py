    def obtener_ultimos(self, mesa_id, n=20):
        """Retorna los últimos n giros de una mesa, ordenados del más reciente al más antiguo.
           Incluye el campo 'id' (Round #)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, api_id, numero, color, tipo, rango, hora, timestamp
                FROM giros
                WHERE mesa_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (mesa_id, n))
            rows = cursor.fetchall()
            resultados = []
            for row in rows:
                resultados.append({
                    "id": row[0],            # Round #
                    "api_id": row[1],
                    "numero": row[2],
                    "color": row[3],
                    "tipo": row[4],
                    "rango": row[5],
                    "hora": row[6],
                    "timestamp": row[7]
                })
            try
                respuesta = requests.get(self.url, headers=headers, timeout=self.timeout)
                if respuesta.status_code == 403:
                    logging.error(f"⚠️ BLOQUEO 403 detectado en {self.url}. Posible bloqueo de la API.")
                    # Opcional: podrías devolver None y manejar un backoff especial
                    return None
                respuesta.raise_for_status()
            return resultados
