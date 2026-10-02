SELECT 
	id, 
	"idVBox", 
	"Sucursal", 
	"Cliente", -- TEXT
	"Area",  -- TEXT
	"NombreMedidor", -- TEXT (RENOMBRA "Medidor")
	"Ciudad", -- TEXT
	"Formato", 
	"Mercado", 
	"Campo", 
	tipo, 
	"M2", 
	"Fecha", 
	"consumoTotal", 
	hora, 
	"ConsumoHora", -- NUMERIC (10, 2)
	tag, -- TEXT
	categoria, -- TEXT
	setpoint_agua -- NUMERIC (20, 2)
	
FROM 
	public.consumo_agua
WHERE 
	"Fecha" > '2026-09-06'
ORDER BY
	id, 
	"idVBox", 
	"Sucursal", 
	"Cliente", 
	"Area", 
	"NombreMedidor", 
	"Ciudad", 
	"Formato", 
	"Fecha", 
	hora;
	