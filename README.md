# DC Accounts

Aplicativo para Windows (PySide6/Qt) que gerencia **várias contas** do
[Remote Desktop Commander](https://github.com/desktop-commander/remote-desktop-commander)
e permite trocar a conta que está ativa no computador com um clique.

> Projeto **não oficial**, sem vínculo com o Desktop Commander. Use por sua conta e risco.

## O que faz

- Cadastra quantas contas você quiser, cada uma com a sua própria sessão salva.
- Ativa uma conta por vez: ao trocar, para a atual de forma graciosa (Ctrl+C) e inicia a nova.
- Mostra o status de cada conta (parada, conectando, online, erro) e o log do agente.
- Abre o link de autorização automaticamente no primeiro login de cada conta.
- Fica na bandeja do sistema, com menu para trocar de conta rapidamente.

## Como funciona

O agente `remote` do Desktop Commander guarda a sessão em
`~/.desktop-commander-device/device.json`. O app mantém um `device.json` por conta e, ao ativar uma
conta, copia o arquivo dela para esse local, inicia o agente e, ao parar, copia de volta os tokens
renovados. As variáveis de ambiente (`HOME`/`USERPROFILE`) **não** são alteradas, então a máquina
remota continua enxergando o seu usuário normalmente.

O comando usado para iniciar o agente é, por padrão:

```
npx -y @wonderwhy-er/desktop-commander@latest remote
```

Dá para alterar em **Configurar comando do device...**.

## Requisitos

- Windows 10/11
- [Node.js](https://nodejs.org) instalado (o app usa o `npx`)

## Uso (executável)

1. Baixe o `DCAccounts.exe` na aba **Releases**.
2. Coloque-o em uma pasta sua (ele cria `accounts.json` e `profiles\` ao lado dele).
3. Abra, clique em **Adicionar conta**, faça o login pelo link que abrir e pronto.
4. Para trocar de conta: selecione-a e clique em **Ativar / trocar**, ou use o menu da bandeja.

## Rodando pelo código

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Compilando o .exe

Execute o `build.bat`. Ele cria o ambiente virtual, instala as dependências, gera o ícone,
compila com o PyInstaller e deixa o `DCAccounts.exe` na raiz da pasta.

## Dados e segurança

`accounts.json`, `profiles\` e `original_device.json` ficam ao lado do `.exe` e contêm
**tokens de acesso das suas contas**. Não compartilhe nem suba essa pasta (já está no `.gitignore`).
Quem tiver esses arquivos consegue conectar à sua máquina como se fosse você.

## Limitações

- Só uma conta fica ativa por vez.
- Se você apagar a sessão (logout) e entrar de novo, o servidor pode criar um novo "device" para a mesma máquina.
- O indicador "Online" é uma heurística baseada na saída do agente.
