// Replace this with your real UI. Common patterns:
//
//   import { Markdown } from './components/Markdown';
//   import { AskChoice } from './components/AskChoice';
//   import { Transcript, Cue } from './components/Transcript';
//
//   export function App() {
//     return (
//       <>
//         <Markdown source="# Hello\n\nWith $LaTeX$" />
//         <AskChoice question="?" choices={['A', 'B']} />
//       </>
//     );
//   }
//
// Copy the widget .tsx files into src/components/ from
// skills/ava-ui/widgets/*/. Install the deps each widget README lists.

export function App() {
  return (
    <div style={{ fontFamily: 'system-ui, sans-serif', maxWidth: 800, margin: '2em auto', padding: '0 1em' }}>
      <h1>Ava React starter</h1>
      <p>
        Replace this content. Copy widget components from{' '}
        <code>skills/ava-ui/widgets/</code> into <code>src/components/</code>.
      </p>
    </div>
  );
}
