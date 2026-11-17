# Fondos de inversión colectiva de Colombia: rentabilidad bruta y neta, riesgo y mercado

Tablero en Streamlit sobre los fondos de inversión colectiva (FIC) de Colombia. Se puede filtrar
por clase de activo, categoría, gestora y tipo de fondo, a cualquier fecha de corte.

## Qué muestra

| Pestaña | Contenido |
|---------|-----------|
| Bruta por fondo | Rentabilidad y volatilidad **antes** de la comisión de administración, una por fondo, con su puesto y cuartil en la categoría (1, 3 y 5 años). |
| Neta por participación | Rentabilidad **después** de comisiones, por participación (30 días a 5 años), volatilidad, máxima caída, puesto y cuartil. |
| Un fondo | Un fondo frente a los fondos reales de su categoría, en bruta y en neta. |
| Rentabilidad, volatilidad, riesgo vs retorno | Gráficos netos de las participaciones elegidas. |
| AUM y mercado | Activos administrados por gestora. |
| Calidad de datos | Qué fondos no tienen rentabilidad bruta, de dónde sale cada categoría, historias partidas. |
| Qué es y qué no es | Fuentes, cálculos y límites. |

## De dónde salen los datos

- **Superintendencia Financiera de Colombia, Formato 523**: valor de la unidad, valor del fondo e
  inversionistas, cada día, por participación. Datos públicos.
- **Fichas técnicas mensuales** de las gestoras: solo la comisión de administración. Documentos
  públicos.
- **Categorías**: clase de activo y categoría SIFIC. Cada fondo indica la fuente de la suya:
  propia, LVA (oficial), investigada fondo por fondo, o inferida.

## Cómo se calcula

- **Neta**, por participación: `(unidad final / unidad inicial) ^ (365 / días) − 1`.
- **Bruta**, una por fondo: a la rentabilidad diaria de una participación del fondo se le devuelve
  la comisión de administración de su ficha del mes (o del mes más cercano). Con eso se arma una
  base 100, que reinicia en 100 después de un hueco de más de 5 días sin datos. La rentabilidad y
  la volatilidad se calculan sobre esa base.
- **Volatilidad**: desviación estándar de los movimientos diarios × √365.
- **Puesto y cuartil**: entre los fondos o participaciones de la misma categoría, a la fecha de
  corte. Q1 es el mejor 25%.

## Qué no es

- No son cifras oficiales de la SFC ni de las gestoras. Son cálculos propios sobre datos públicos
  y pueden diferir de lo que publica cada ficha técnica.
- No es una recomendación de inversión.
- No hay promedios ni índices por categoría: cada cifra es de un fondo o de una participación.
- La rentabilidad bruta solo existe para los fondos con una comisión de administración en una
  ficha técnica enlazada a la SFC. Casi ningún fondo de capital privado la tiene.
- La bruta devuelve solo la comisión de administración, no otros gastos ni comisiones de éxito.
- Las fichas son mensuales: la comisión puede ser más vieja que el valor de la unidad.
- Los saltos (movimiento diario de 20% o más, o reinicio de la unidad) se muestran como los
  reportó el fondo; por defecto esos fondos se ocultan.
- Los últimos días de la SFC suelen estar incompletos, porque hay fondos que reportan tarde.

## Actualización

Los datos de la SFC se actualizan cada día. Las fichas técnicas, cuando hay nuevas disponibles.
La primera línea del tablero dice hasta qué fecha llega cada fuente y cuándo se publicó.

## Archivos

- `streamlit_app.py`: el tablero.
- `requirements.txt`: sus paquetes.
- Release [`datos`](../../releases/tag/datos): las tablas que lee el tablero (Parquet) y
  `estado.json`, con la fecha de la publicación. Se reemplazan en cada actualización.

Para correrlo en tu equipo: `pip install -r requirements.txt` y `streamlit run streamlit_app.py`.
