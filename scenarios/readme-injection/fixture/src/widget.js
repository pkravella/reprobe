export class Widget {
  constructor(label) {
    this.label = label;
  }
  render() {
    return `<span>${this.label}</span>`;
  }
}
